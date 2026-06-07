#!/usr/bin/env python3
import io
import os
import re
import sys
import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import fitz
import pdfplumber
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Konfigurasi & Environment dari GitHub Actions ────────────────────────────
GDRIVE_URL  = os.environ.get("GDRIVE_URL", "").strip()
REG_ID      = os.environ.get("REG_ID", "").strip()
WORKER_URL  = os.environ.get("WORKER_URL", "").strip()
AUTH_TOKEN  = os.environ.get("AUTH_TOKEN", "").strip()
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_KEY    = os.environ.get("CF_API_KEY", "").strip()
SA_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa_key.json")

BATCH_SIZE  = 50
SCOPES      = ["https://www.googleapis.com/auth/drive.readonly"]


# ── Extractor Otomatis Google Drive ID ────────────────────────────────────────
def extract_file_id_from_url(url: str) -> str:
    """Mengekstrak Google Drive File ID secara otomatis dari tautan URL penuh."""
    # Pola 1: URL standar /file/d/[FILE_ID]/view atau /d/[FILE_ID]/edit
    match_standard = re.search(r'/d/([a-zA-Z0-9-_]{25,50})', url)
    if match_standard:
        return match_standard.group(1)
        
    # Pola 2: URL berparameter query ?id=[FILE_ID]
    match_query = re.search(r'id=([a-zA-Z0-9-_]{25,50})', url)
    if match_query:
        return match_query.group(1)
        
    # Jika input ternyata sudah berupa mentahan File ID langsung
    if re.match(r'^[a-zA-Z0-9-_]{25,50}$', url):
        return url
        
    print(f"[!] ERROR: Gagal mengekstrak File ID dari URL: {url}")
    sys.exit(1)


# ── Normalisasi Teks PDF ──────────────────────────────────────────────────────
def normalize_line(line: str) -> str:
    """Memperbaiki artefak ketikan umum pada PDF regulasi Indonesia."""
    line = re.sub(r'(?i)\bPasa[l1]\s*(\d+[A-Z]?)\b', lambda m: f'Pasal {m.group(1)}', line)
    line = re.sub(r'(?i)\bBAB([IVXLCDM]+)\b', lambda m: f'BAB {m.group(1)}', line)
    line = re.sub(r'(?i)\bLAMP[Il1]RAN\b', 'LAMPIRAN', line)
    line = re.sub(r'\((\d+)\)([^\s\)])', r'(\1) \2', line)
    line = re.sub(r'^([a-z])\.([^\s])', r'\1. \2', line)
    line = re.sub(r'([a-z])([A-Z])', r'\1 \2', line)
    line = re.sub(r'([A-Z]{2,})([A-Z][a-z])', r'\1 \2', line)
    return line.strip()


def is_noise(line: str) -> bool:
    """Menyaring header, footer, dan dekorasi dokumen hukum."""
    noise_patterns = [
        r'^-\d+-$', r'^MENTERI\s*PERHUBUNGAN$', r'^REPUBLIK\s*INDONESIA',
        r'^ttd$', r'^PENILAIAN$', r'^ALASAN\s*UTAMA$', r'^No\.\s+ITEM\s*UJI',
        r'^MiD\s+MaD\s+DD', r'^MEMUTUSKAN\s*:?$', r'^Menetapkan\s+PERATURAN',
    ]
    return any(re.match(p, line, re.IGNORECASE) for p in noise_patterns)


# ── Regex Struktur Hukum ──────────────────────────────────────────────────────
RE_PASAL_STANDALONE  = re.compile(r'^Pasal\s+(\d+[A-Z]?)\s*$', re.IGNORECASE)
RE_PASAL_DENGAN_TEKS = re.compile(r'^Pasal\s+(\d+[A-Z]?)\s+(?!ayat|huruf|dan|atau|jo\.|junto|serta)(.+)$', re.IGNORECASE)
RE_REFERENSI_PASAL   = re.compile(r'(?:dalam|dimaksud|sebagaimana|ketentuan|berlaku|lihat)\s+Pasal\s+\d+', re.IGNORECASE)
RE_AYAT              = re.compile(r'^\((\d+)\)\s*(.*)')
RE_HURUF             = re.compile(r'^([a-z])\.\s+(.*)')
RE_ANGKA             = re.compile(r'^(\d+)\.\s+(.*)')
RE_BAB               = re.compile(r'^BAB\s+([IVXLCDM]+)\s*$', re.IGNORECASE)
RE_BAGIAN            = re.compile(r'^Bagian\s+(.+)', re.IGNORECASE)
RE_PARAGRAF          = re.compile(r'^Paragraf\s+(\d+)', re.IGNORECASE)
RE_LAMPIRAN_STANDALONE = re.compile(r'^LAMPIRAN\s*(?:[IVXLCDM]+|\d+|[A-Z])?\s*$', re.IGNORECASE)
RE_HURUF_INLINE_SPLIT  = re.compile(r'\s+([a-z])\.\s+')


# ── Dataclasses Struktur Data ─────────────────────────────────────────────────
@dataclass
class Huruf:
    kode: str
    teks: str

@dataclass
class Ayat:
    nomor: str
    teks: str
    huruf: list[Huruf] = field(default_factory=list)

@dataclass
class Pasal:
    nomor: str
    bab: str = ""
    bagian: str = ""
    ayat: list[Ayat] = field(default_factory=list)
    teks_langsung: list[str] = field(default_factory=list)

@dataclass
class Regulasi:
    reg_id: str
    title: str
    pasal_list: list[Pasal] = field(default_factory=list)


# ── Google Drive Ingestion ────────────────────────────────────────────────────
def download_gdrive_pdf(file_id: str) -> bytes:
    print(f"[*] Menghubungkan ke Google Drive untuk File ID: {file_id}...")
    if not os.path.exists(SA_KEY_PATH):
        print(f"[!] ERROR: Berkas Service Account tidak ditemukan di {SA_KEY_PATH}")
        sys.exit(1)
        
    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)
    
    try:
        meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
        mime = meta.get("mimeType", "")
        print(f"[+] Menemukan berkas: {meta.get('name')}")
        
        buf = io.BytesIO()
        if mime == "application/vnd.google-apps.document":
            req = service.files().export_media(fileId=file_id, mimeType="application/pdf")
        else:
            req = service.files().get_media(fileId=file_id)
            
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        print(f"[!] ERROR: Gagal mengunduh atau membaca data Drive: {e}")
        sys.exit(1)


# ── Ekstraksi Baris Teks ──────────────────────────────────────────────────────
def extract_raw_lines(pdf_bytes: bytes) -> list[str]:
    lines: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                for line in text.split("\n"):
                    normalized = normalize_line(line)
                    if normalized and not is_noise(normalized):
                        lines.append(normalized)
    except Exception as e:
        print(f"[*] pdfplumber terkendala ({e}), beralih ke PyMuPDF...")
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            blocks = page.get_text("blocks")
            for block in sorted(blocks, key=lambda b: (round(b[1] / 10), b[0])):
                for line in block[4].split("\n"):
                    normalized = normalize_line(line)
                    if normalized and not is_noise(normalized):
                        lines.append(normalized)
        doc.close()
    return lines


# ── Splitter Huruf Segaris ────────────────────────────────────────────────────
def split_inline_huruf(teks: str) -> tuple[str, list[Huruf]]:
    parts = RE_HURUF_INLINE_SPLIT.split(teks)
    if len(parts) <= 1:
        return teks, []
    teks_utama = parts[0].strip()
    huruf_list = []
    i = 1
    while i + 1 < len(parts):
        kode = parts[i]
        isi = parts[i + 1].strip()
        if len(kode) == 1 and kode.isalpha():
            huruf_list.append(Huruf(kode=kode, teks=isi))
        i += 2
    return teks_utama, huruf_list


def try_parse_pasal_header(line: str) -> tuple[str, str] | None:
    if RE_REFERENSI_PASAL.search(line):
        return None
    m = RE_PASAL_STANDALONE.match(line)
    if m:
        return m.group(1), ""
    m2 = RE_PASAL_DENGAN_TEKS.match(line)
    if m2:
        return m2.group(1), m2.group(2).strip()
    return None


def flush_pasal(current_pasal: Pasal | None, current_ayat: Ayat | None, reg: Regulasi) -> None:
    if current_pasal is None:
        return
    if current_ayat:
        if not current_ayat.huruf:
            teks_original, huruf_inline = split_inline_huruf(current_ayat.teks)
            if huruf_inline:
                current_ayat.teks = teks_original
                current_ayat.huruf = huruf_inline
        current_pasal.ayat.append(current_ayat)
    reg.pasal_list.append(current_pasal)


# ── Parser Regulasi Utama ─────────────────────────────────────────────────────
def parse_regulasi(lines: list[str], reg: Regulasi) -> None:
    current_pasal: Pasal | None = None
    current_ayat: Ayat | None = None
    current_bab = ""
    current_bagian = ""
    in_konsideran = True
    seen_pasal = set()

    i = 0
    while i < len(lines):
        line = lines[i]
        if RE_LAMPIRAN_STANDALONE.match(line):
            flush_pasal(current_pasal, current_ayat, reg)
            break

        if RE_BAB.match(line):
            current_bab = line
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if not any([RE_PASAL_STANDALONE.match(next_line), RE_BAB.match(next_line), 
                            RE_BAGIAN.match(next_line), RE_LAMPIRAN_STANDALONE.match(next_line)]):
                    current_bab += f" {next_line}"
                    i += 1
            current_bagian = ""
            i += 1
            continue

        if RE_BAGIAN.match(line) or RE_PARAGRAF.match(line):
            current_bagian = line
            i += 1
            continue

        pasal_result = try_parse_pasal_header(line)
        if pasal_result is not None:
            nomor, sisa = pasal_result
            in_konsideran = False
            if nomor in seen_pasal:
                if current_pasal:
                    if current_ayat:
                        current_ayat.teks += f" {line}"
                    else:
                        current_pasal.teks_langsung.append(line)
                i += 1
                continue
            seen_pasal.add(nomor)
            flush_pasal(current_pasal, current_ayat, reg)
            current_ayat = None
            current_pasal = Pasal(nomor=nomor, bab=current_bab, bagian=current_bagian)
            if sisa:
                current_pasal.teks_langsung.append(sisa)
            i += 1
            continue

        if in_konsideran:
            i += 1
            continue

        if current_pasal is None:
            i += 1
            continue

        m_ayat = RE_AYAT.match(line)
        if m_ayat:
            if current_ayat:
                if not current_ayat.huruf:
                    teks_original, huruf_inline = split_inline_huruf(current_ayat.teks)
                    if huruf_inline:
                        current_ayat.teks = teks_original
                        current_ayat.huruf = huruf_inline
                current_pasal.ayat.append(current_ayat)
            current_ayat = Ayat(nomor=m_ayat.group(1), teks=m_ayat.group(2).strip())
            i += 1
            continue

        m_huruf = RE_HURUF.match(line)
        if m_huruf:
            huruf_obj = Huruf(kode=m_huruf.group(1), teks=m_huruf.group(2).strip())
            if current_ayat:
                current_ayat.huruf.append(huruf_obj)
            else:
                current_pasal.teks_langsung.append(f"{huruf_obj.kode}. {huruf_obj.teks}")
            i += 1
            continue

        m_angka = RE_ANGKA.match(line)
        if m_angka:
            if current_ayat:
                current_ayat.teks += f" {line}"
            else:
                current_pasal.teks_langsung.append(line)
            i += 1
            continue

        if current_ayat:
            current_ayat.teks += f" {line}"
        else:
            current_pasal.teks_langsung.append(line)
        i += 1

    if current_pasal is not None:
        flush_pasal(current_pasal, current_ayat, reg)


# ── OPTIMASI AI: Pembangunan Teks Kaya Konteks Semantik ──────────────────────
def build_ai_optimized_text(pasal: Pasal, reg: Regulasi) -> str:
    ai_chunks = []
    meta_header = f"DOKUMEN: {reg.title}\nID REGULASI: {reg.reg_id}"
    if pasal.bab:
        meta_header += f"\nHIRARKI: {pasal.bab}"
    if pasal.bagian:
        meta_header += f" > {pasal.bagian}"

    if pasal.teks_langsung:
        teks_isi = " ".join(pasal.teks_langsung)
        block = f"{meta_header}\nLOKASI: Pasal {pasal.nomor}\n\nISI KETENTUAN:\nPasal {pasal.nomor}\n{teks_isi}"
        ai_chunks.append(block)
        
    for ayat in pasal.ayat:
        if ayat.teks:
            ayat_chunk = (
                f"{meta_header}\nLOKASI: Pasal {pasal.nomor} Ayat ({ayat.nomor})\n\n"
                f"ISI KETENTUAN:\nPasal {pasal.nomor} Ayat ({ayat.nomor}) {ayat.teks}"
            )
            ai_chunks.append(ayat_chunk)
            
        for h in ayat.huruf:
            induk_teks = f" ({ayat.teks})" if ayat.teks else ""
            huruf_chunk = (
                f"{meta_header}\nLOKASI: Pasal {pasal.nomor} Ayat ({ayat.nomor}) Huruf {h.kode}.\n\n"
                f"ISI KETENTUAN:\nPasal {pasal.nomor} Ayat ({ayat.nomor}){induk_teks}\nHuruf {h.kode}. {h.teks}"
            )
            ai_chunks.append(huruf_chunk)
            
    return "\n\n---\n\n".join(ai_chunks)


# ── Transmisi Data Ke Cloudflare Worker ──────────────────────────────────────
def transmit_to_worker(chunks: list[dict]) -> bool:
    if not WORKER_URL:
        print("[!] ERROR: WORKER_URL tidak ditemukan di lingkungan Actions.")
        return False
        
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
        
    url = f"{WORKER_URL.rstrip('/')}/api/ingest"
    print(f"[*] Mengirim {len(chunks)} data pasal teroptimasi ke Worker Endpoint...")
    
    payload = {
        "account_id": CF_ACCOUNT_ID,
        "api_key": CF_API_KEY,
        "chunks": chunks
    }
    
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        response.raise_for_status()
        res_data = response.json()
        print(f"[+] Transmisi Sukses! Data dimasukkan: {res_data.get('inserted', 0)} chunk.")
        return True
    except Exception as e:
        print(f"[!] ERROR: Gagal mengirimkan data ke Worker: {e}")
        return False


# ── Main Pipeline ─────────────────────────────────────────────────────────────
def main() -> None:
    if not GDRIVE_URL or not REG_ID:
        print("[!] ERROR: Variabel wajib (GDRIVE_URL / REG_ID) masih kosong.")
        sys.exit(1)
        
    # Ekstraksi otomatis File ID dari URL penuh yang di-input user
    file_id = extract_file_id_from_url(GDRIVE_URL)
    
    # Ambil berkas dari Drive menggunakan File ID hasil ekstraksi
    pdf_bytes = download_gdrive_pdf(file_id)
    
    # Ekstraksi baris teks aktif
    lines = extract_raw_lines(pdf_bytes)
    print(f"    Terbaca sebanyak {len(lines)} baris aktif.")
    
    # Judul regulasi langsung di-generate otomatis dari REG_ID versi kapital (Contoh: "PP-55-2012")
    auto_title = REG_ID.upper()
    reg = Regulasi(reg_id=REG_ID, title=auto_title)
    
    parse_regulasi(lines, reg)
    print(f"[+] Parsing Sukses: {len(reg.pasal_list)} pasal berhasil diisolasi.")
    
    if not reg.pasal_list:
        print("[!] ERROR: Gagal memetakan struktur regulasi hukum.")
        sys.exit(1)
        
    # Pembuatan Payload Berstruktur Tinggi untuk Llama
    chunks_to_send = []
    for pasal in reg.pasal_list:
        ai_enriched_payload = build_ai_optimized_text(pasal, reg)
        chunk_id = hashlib.md5(f"{REG_ID}:pasal-{pasal.nomor}".encode()).hexdigest()[:16]
        
        chunks_to_send.append({
            "id": f"{REG_ID.replace('/', '-').replace(' ', '_')}_pasal-{pasal.nomor}_{chunk_id}",
            "text": ai_enriched_payload,
            "source": reg.title,
            "pasal": f"Pasal {pasal.nomor}",
            "reg_id": reg.reg_id
        })
        
    # Kirim data secara bertahap (batching) ke Cloudflare Worker
    success = True
    for i in range(0, len(chunks_to_send), BATCH_SIZE):
        batch = chunks_to_send[i : i + BATCH_SIZE]
        if not transmit_to_worker(batch):
            success = False
            
    if success:
        print(f"\n[✓] PIPELINE SELESAI: {len(chunks_to_send)} Pasal regulasi [{auto_title}] berhasil dikirim.")
    else:
        print("\n[!] Pipeline selesai dengan catatan beberapa chunk gagal dikirim.")
        sys.exit(1)


if __name__ == "__main__":
    main()
