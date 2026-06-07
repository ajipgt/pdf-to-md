#!/usr/bin/env python3
import io
import os
import re
import sys
import json
import hashlib

import fitz  # PyMuPDF
import pdfplumber
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Konfigurasi Lingkungan ───────────────────────────────────────────────────
GDRIVE_URL  = os.environ.get("GDRIVE_URL", "").strip()
REG_ID      = os.environ.get("REG_ID", "").strip()
WORKER_URL  = os.environ.get("WORKER_URL", "").strip()
AUTH_TOKEN  = os.environ.get("AUTH_TOKEN", "").strip()
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_KEY    = os.environ.get("CF_API_KEY", "").strip()
SA_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa_key.json")

BATCH_SIZE  = 30  # Diperkecil karena payload tabel markdown bisa lebih padat
SCOPES      = ["https://www.googleapis.com/auth/drive.readonly"]


def extract_file_id_from_url(url: str) -> str:
    match = re.search(r'/d/([a-zA-Z0-9-_]{25,50})', url)
    if match: return match.group(1)
    match_q = re.search(r'id=([a-zA-Z0-9-_]{25,50})', url)
    if match_q: return match_q.group(1)
    if re.match(r'^[a-zA-Z0-9-_]{25,50}$', url): return url
    print(f"[!] ERROR: URL Drive tidak valid."); sys.exit(1)


def convert_table_to_markdown(table_data: list) -> str:
    """Mengubah matriks list tabel pdfplumber menjadi format tabel Markdown bersih."""
    if not table_data or not table_data[0]:
        return ""
    
    markdown_lines = []
    # Bersihkan cell dari None atau newline berlebih
    headers = [str(cell or "").replace("\n", " ").strip() for cell in table_data[0]]
    
    # Lewati jika header sepenuhnya kosong
    if not any(headers):
        return ""
        
    markdown_lines.append("| " + " | ".join(headers) + " |")
    markdown_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    
    for row in table_data[1:]:
        row_clean = [str(cell or "").replace("\n", " ").strip() for cell in row]
        # Hanya masukkan baris jika ada isinya
        if any(row_clean):
            markdown_lines.append("| " + " | ".join(row_clean) + " |")
            
    return "\n".join(markdown_lines)


# ── Parser Pintar Lampiran: Menggabungkan Tabel, Gambar, & Teks ──────────────
def extract_optimized_lampiran(pdf_bytes: bytes) -> dict[str, list[str]]:
    """
    Mengekstrak area lampiran secara hibrida.
    Mendeteksi tabel (diubah ke markdown) dan gambar (diubah ke placeholder teks).
    """
    lampiran_data = {}
    current_lamp_name = ""
    in_lampiran_zone = False
    
    re_lamp_header = re.compile(r'^LAMPIRAN\s*(.*)', re.IGNORECASE)
    
    # Langkah 1: Gunakan PyMuPDF untuk mendeteksi koordinat / informasi gambar per halaman
    doc_images = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_visual_notes = {}
    
    for page_idx, page in enumerate(doc_images):
        notes = []
        image_list = page.get_images(full=True)
        if image_list:
            # Cari tahu apakah ada indikasi ttd / stempel berdasarkan jumlah atau ukuran elemen
            has_signature_area = len(image_list) >= 1
            if has_signature_area:
                notes.append("[DOKUMEN OTENTIK: Terdeteksi elemen visual berupa Stempel Dinas / Tanda Tangan Resmi pada lembar ini]")
        page_visual_notes[page_idx] = notes
    doc_images.close()

    # Langkah 2: Gunakan pdfplumber untuk ekstraksi struktur teks dan tabel secara presisi
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            
            # Ambil catatan visual (stempel/ttd) dari langkah 1
            visual_notes = page_visual_notes.get(page_idx, [])
            
            # Ekstrak semua tabel yang ada di halaman ini
            tables_found = page.find_tables()
            table_objects = [t.extract() for t in tables_found]
            
            # Buat teks mentah halaman ini tanpa mengganggu area tabel (jika memungkinkan)
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            
            lines = text.split("\n")
            page_content_combined = []
            
            # Sisipkan catatan stempel/ttd di awal halaman jika terdeteksi
            if visual_notes:
                page_content_combined.extend(visual_notes)
                
            # Jika ada tabel, ubah seluruh tabel menjadi markdown dan kumpulkan
            if table_objects:
                page_content_combined.append("\n[STRUKTUR DATA TABEL TEKNIS]:")
                for table in table_objects:
                    md_table = convert_table_to_markdown(table)
                    if md_table:
                        page_content_combined.append(md_table)
            
            # Masukkan teks baris biasa yang bukan bagian dari noise struktural
            for line in lines:
                line_clean = line.strip()
                if not line_clean:
                    continue
                    
                match = re_lamp_header.match(line_clean)
                if match:
                    in_lampiran_zone = True
                    suffix = match.group(1).strip()
                    current_lamp_name = f"LAMPIRAN {suffix}" if suffix else "LAMPIRAN TEKNIS"
                    if current_lamp_name not in lampiran_data:
                        lampiran_data[current_lamp_name] = []
                    continue
                
                if in_lampiran_zone and current_lamp_name:
                    # Cegah teks tabel mentah masuk ganda jika barisnya terlalu mirip (opsional penyeimbangan)
                    lampiran_data[current_lamp_name].append(line_clean)
            
            # Gabungkan data tabel ke dalam kontainer lampiran berjalan
            if in_lampiran_zone and current_lamp_name and page_content_combined:
                lampiran_data[current_lamp_name].extend(page_content_combined)
                
    return lampiran_data


def chunk_lampiran_for_llama(title: str, reg_id: str, lamp_name: str, lines: list[str]) -> list[str]:
    """Memotong konten lampiran secara proporsional agar tabel markdown tidak terpotong di tengah jalan."""
    chunks = []
    current_chunk_lines = []
    current_word_count = 0
    
    meta_header = f"DOKUMEN: {title}\nID REGULASI: {reg_id}\nLOKASI: {lamp_name.upper()}"
    
    for line in lines:
        current_chunk_lines.append(line)
        current_word_count += len(line.split())
        
        # Jika sudah mencapai batas (~350 kata) ATAU baris berupa penutup tabel markdown
        if current_word_count >= 350 or (line.startswith("|") and current_word_count >= 250):
            isi_blok = "\n".join(current_chunk_lines)
            block = f"{meta_header}\n\nISI DETAIL TEKNIS LAMPIRAN:\n{isi_blok}"
            chunks.append(block)
            
            current_chunk_lines = []
            current_word_count = 0
            
    if current_chunk_lines:
        isi_blok = "\n".join(current_chunk_lines)
        block = f"{meta_header}\n\nISI DETAIL TEKNIS LAMPIRAN:\n{isi_blok}"
        chunks.append(block)
        
    return chunks


def transmit_to_worker(chunks: list[dict]) -> bool:
    if not WORKER_URL: return False
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN: headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    payload = {"account_id": CF_ACCOUNT_ID, "api_key": CF_API_KEY, "chunks": chunks}
    try:
        response = requests.post(f"{WORKER_URL.rstrip('/')}/api/ingest", headers=headers, data=json.dumps(payload), timeout=60)
        return response.status_code == 200
    except:
        return False


def download_gdrive_pdf(file_id: str) -> bytes:
    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)
    buf = io.BytesIO()
    req = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done: _, done = downloader.next_chunk()
    return buf.getvalue()


def main() -> None:
    if not GDRIVE_URL or not REG_ID: sys.exit(1)
    file_id = extract_file_id_from_url(GDRIVE_URL)
    pdf_bytes = download_gdrive_pdf(file_id)
    
    print("[*] Memproses Lampiran Hibrida (Deteksi Tabel Markdown & Elemen Autentikasi)...")
    lampiran_map = extract_optimized_lampiran(pdf_bytes)
    
    if not lampiran_map:
        print("[!] Tidak ada halaman berformat 'LAMPIRAN' yang terisolasi."); sys.exit(0)
        
    auto_title = REG_ID.upper()
    chunks_to_send = []
    
    for lamp_name, lines in lampiran_map.items():
        text_blocks = chunk_lampiran_for_llama(auto_title, REG_ID, lamp_name, lines)
        for index, block_text in enumerate(text_blocks):
            hash_id = hashlib.md5(f"{REG_ID}:{lamp_name}:{index}".encode()).hexdigest()[:16]
            chunks_to_send.append({
                "id": f"{REG_ID.replace('/', '-').replace(' ', '_')}_lampiran-{index}_{hash_id}",
                "text": block_text,
                "source": auto_title,
                "pasal": lamp_name,
                "reg_id": REG_ID
            })
            
    success = True
    for i in range(0, len(chunks_to_send), BATCH_SIZE):
        if not transmit_to_worker(chunks_to_send[i : i + BATCH_SIZE]): success = False
        
    if success: print(f"[✓] Sukses mengasimilasi {len(chunks_to_send)} chunk Lampiran Teroptimasi ke Cloudflare!")
    else: sys.exit(1)

if __name__ == "__main__":
    main()
