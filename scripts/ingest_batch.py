#!/usr/bin/env python3
"""
Orchestrator ingest batch seluruh folder GDrive.
Dipanggil oleh GH Actions workflow, bukan langsung oleh user.

Flow per file:
  1. List semua PDF di folder GDrive
  2. Download PDF
  3. Jalankan pipeline pasal (ingest_pasal)
  4. Jalankan pipeline lampiran (ingest_lampiran)
  5. Lanjut file berikutnya
"""

import io
import os
import re
import sys
import json
import hashlib
import importlib.util
from pathlib import Path
from dataclasses import dataclass, field

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Konfigurasi dari GH Actions ───────────────────────────────────────────────
GDRIVE_FOLDER_URL = os.environ.get("GDRIVE_FOLDER_URL", "").strip()
WORKER_URL        = os.environ.get("WORKER_URL", "").strip()
AUTH_TOKEN        = os.environ.get("AUTH_TOKEN", "").strip()
CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_KEY        = os.environ.get("CF_API_KEY", "").strip()
SA_KEY_PATH       = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa_key.json")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


# ── Ekstrak Folder ID dari URL ────────────────────────────────────────────────
def extract_folder_id(url: str) -> str:
    # Pola: /folders/[ID]
    match = re.search(r'/folders/([a-zA-Z0-9-_]{25,50})', url)
    if match:
        return match.group(1)
    # Input langsung berupa ID
    if re.match(r'^[a-zA-Z0-9-_]{25,50}$', url):
        return url
    print(f"[!] ERROR: Gagal mengekstrak Folder ID dari: {url}")
    sys.exit(1)


# ── Auto-generate reg_id dari nama file ──────────────────────────────────────
def filename_to_reg_id(filename: str) -> str:
    """
    'PM Kemenhub 133 Tahun 2015.pdf' -> 'pm-kemenhub-133-2015'
    'PP 55 Tahun 2012.pdf'           -> 'pp-55-2012'
    'UU 22 Tahun 2009.pdf'           -> 'uu-22-2009'
    """
    stem = Path(filename).stem
    # Hapus kata "Tahun" dan "Dirjenhubdat" untuk bersihkan nama
    stem = re.sub(r'\bTahun\b', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\bDirjenhubdat\b', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\bKemenhub\b', 'Kemenhub', stem, flags=re.IGNORECASE)
    # Ganti spasi dan karakter non-alphanumeric dengan dash
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', stem.strip())
    slug = slug.strip('-').lower()
    # Hapus dash berulang
    slug = re.sub(r'-+', '-', slug)
    return slug


# ── GDrive: List semua PDF di folder ─────────────────────────────────────────
def list_pdfs_in_folder(service, folder_id: str) -> list[dict]:
    """Return list of {id, name} untuk semua PDF di folder."""
    results = []
    page_token = None

    query = (
        f"'{folder_id}' in parents "
        f"and mimeType='application/pdf' "
        f"and trashed=false"
    )

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            orderBy="name",
        ).execute()

        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def download_pdf(service, file_id: str) -> bytes:
    buf = io.BytesIO()
    req = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ── Dynamic import ingest_pasal & ingest_lampiran ────────────────────────────
def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(mod)  # type: ignore
    return mod


# ── Pipeline per file ─────────────────────────────────────────────────────────
def run_pasal_pipeline(mod_pasal, pdf_bytes: bytes, reg_id: str) -> tuple[bool, int]:
    """Jalankan pipeline ingest_pasal untuk satu PDF."""
    try:
        lines = mod_pasal.extract_raw_lines(pdf_bytes)
        print(f"    [pasal] {len(lines)} baris aktif terbaca")

        reg = mod_pasal.Regulasi(reg_id=reg_id, title=reg_id.upper())
        mod_pasal.parse_regulasi(lines, reg)
        print(f"    [pasal] {len(reg.pasal_list)} pasal berhasil diparsing")

        if not reg.pasal_list:
            print("    [pasal] ⚠ Tidak ada pasal ditemukan, skip.")
            return True, 0

        chunks = []
        for pasal in reg.pasal_list:
            ai_text = mod_pasal.build_ai_optimized_text(pasal, reg)
            chunk_id = hashlib.md5(f"{reg_id}:pasal-{pasal.nomor}".encode()).hexdigest()[:16]
            chunks.append({
                "id": f"{reg_id}_pasal-{pasal.nomor}_{chunk_id}",
                "text": ai_text,
                "source": reg_id.upper(),
                "pasal": f"Pasal {pasal.nomor}",
                "reg_id": reg_id,
            })

        # Kirim ke worker dalam batch
        success = transmit_chunks(chunks)
        return success, len(chunks)

    except Exception as e:
        print(f"    [pasal] ❌ Error: {e}")
        return False, 0


def run_lampiran_pipeline(mod_lamp, pdf_bytes: bytes, reg_id: str) -> tuple[bool, int]:
    """Jalankan pipeline ingest_lampiran untuk satu PDF."""
    try:
        lampiran_map = mod_lamp.extract_optimized_lampiran(pdf_bytes)

        if not lampiran_map:
            print("    [lampiran] Tidak ada lampiran ditemukan, skip.")
            return True, 0

        print(f"    [lampiran] {len(lampiran_map)} lampiran ditemukan")

        chunks = []
        for lamp_name, lines in lampiran_map.items():
            text_blocks = mod_lamp.chunk_lampiran_for_llama(
                reg_id.upper(), reg_id, lamp_name, lines
            )
            for idx, block_text in enumerate(text_blocks):
                hash_id = hashlib.md5(f"{reg_id}:{lamp_name}:{idx}".encode()).hexdigest()[:16]
                chunks.append({
                    "id": f"{reg_id}_lampiran-{idx}_{hash_id}",
                    "text": block_text,
                    "source": reg_id.upper(),
                    "pasal": lamp_name,
                    "reg_id": reg_id,
                })

        success = transmit_chunks(chunks)
        return success, len(chunks)

    except Exception as e:
        print(f"    [lampiran] ❌ Error: {e}")
        return False, 0


# ── Transmit ke Worker ────────────────────────────────────────────────────────
def transmit_chunks(chunks: list[dict], batch_size: int = 10) -> bool:
    if not WORKER_URL:
        print("    ❌ WORKER_URL tidak ditemukan")
        return False

    import requests as req_lib

    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"

    url = f"{WORKER_URL.rstrip('/')}/api/ingest"
    total = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]

        # Truncate chunk teks yang terlalu panjang — CF Worker limit ~1MB per request
        # build_ai_optimized_text bisa sangat panjang untuk pasal dengan banyak ayat+huruf
        for chunk in batch:
            if len(chunk["text"]) > 8000:
                chunk["text"] = chunk["text"][:8000] + "\n...[terpotong]"

        payload = {
            "account_id": CF_ACCOUNT_ID,
            "api_key": CF_API_KEY,
            "chunks": batch,
        }

        payload_size = len(json.dumps(payload))
        if payload_size > 900_000:  # 900KB safety margin
            # Split lebih kecil lagi
            mid = len(batch) // 2
            ok1 = transmit_chunks(batch[:mid], batch_size=1)
            ok2 = transmit_chunks(batch[mid:], batch_size=1)
            if not (ok1 and ok2):
                return False
            continue

        try:
            resp = req_lib.post(url, headers=headers, data=json.dumps(payload), timeout=60)
            resp.raise_for_status()
            total += resp.json().get("inserted", 0)
        except Exception as e:
            print(f"    ❌ Gagal kirim batch: {e}")
            return False

    print(f"    ✅ {total} chunk berhasil dikirim ke Vectorize")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not GDRIVE_FOLDER_URL:
        print("[!] ERROR: GDRIVE_FOLDER_URL tidak ditemukan")
        sys.exit(1)

    folder_id = extract_folder_id(GDRIVE_FOLDER_URL)
    print(f"[*] Folder ID: {folder_id}")

    # Load GDrive service
    if not os.path.exists(SA_KEY_PATH):
        print(f"[!] ERROR: Service Account key tidak ditemukan di {SA_KEY_PATH}")
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)

    # List semua PDF di folder
    pdf_files = list_pdfs_in_folder(service, folder_id)
    if not pdf_files:
        print("[!] Tidak ada file PDF ditemukan di folder")
        sys.exit(1)

    print(f"[+] Ditemukan {len(pdf_files)} file PDF\n")

    # Load module pasal & lampiran secara dinamis
    scripts_dir = Path(__file__).parent
    mod_pasal = load_module("ingest_pasal", str(scripts_dir / "ingest_pasal.py"))
    mod_lamp  = load_module("ingest_lampiran", str(scripts_dir / "ingest_lampiran.py"))

    # Hasil summary
    results = []
    total_failed = 0

    for idx, pdf_file in enumerate(pdf_files, 1):
        file_name = pdf_file["name"]
        file_id   = pdf_file["id"]
        reg_id    = filename_to_reg_id(file_name)

        print(f"[{idx}/{len(pdf_files)}] {file_name}")
        print(f"    reg_id: {reg_id}")

        # Download PDF
        try:
            pdf_bytes = download_pdf(service, file_id)
            print(f"    Downloaded: {len(pdf_bytes) / 1024:.1f} KB")
        except Exception as e:
            print(f"    ❌ Gagal download: {e}")
            results.append({"file": file_name, "status": "GAGAL (download)"})
            total_failed += 1
            continue

        # Pipeline pasal
        pasal_ok, pasal_count = run_pasal_pipeline(mod_pasal, pdf_bytes, reg_id)

        # Pipeline lampiran
        lamp_ok, lamp_count = run_lampiran_pipeline(mod_lamp, pdf_bytes, reg_id)

        status = "✅ OK" if (pasal_ok and lamp_ok) else "⚠ Partial"
        results.append({
            "file": file_name,
            "reg_id": reg_id,
            "pasal_chunks": pasal_count,
            "lampiran_chunks": lamp_count,
            "status": status,
        })

        if not (pasal_ok and lamp_ok):
            total_failed += 1

        print()

    # Summary
    print("=" * 60)
    print("SUMMARY INGEST BATCH")
    print("=" * 60)
    for r in results:
        print(f"  {r['status']} {r['file']}")
        if "pasal_chunks" in r:
            print(f"       Pasal: {r['pasal_chunks']} chunk | Lampiran: {r['lampiran_chunks']} chunk")

    print(f"\nTotal file: {len(pdf_files)} | Gagal: {total_failed}")

    # Output JSON untuk GH Actions summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## 📦 Hasil Ingest Batch Regulasi\n\n")
            f.write("| File | reg_id | Pasal | Lampiran | Status |\n")
            f.write("|------|--------|-------|----------|--------|\n")
            for r in results:
                f.write(
                    f"| {r['file']} | `{r.get('reg_id', '-')}` | "
                    f"{r.get('pasal_chunks', '-')} | {r.get('lampiran_chunks', '-')} | "
                    f"{r['status']} |\n"
                )

    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
