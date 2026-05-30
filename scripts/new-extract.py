import io
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import fitz
import pdfplumber
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Konfigurasi ──────────────────────────────────────────────────────────────
FILE_ID    = os.environ.get("GDRIVE_FILE_ID", "").strip()
REG_ID     = os.environ.get("REG_ID", "").strip()
REG_TITLE  = os.environ.get("REG_TITLE", "").strip()
REG_TYPE   = os.environ.get("REG_TYPE", "").strip()
REG_STATUS = os.environ.get("REG_STATUS", "berlaku").strip()
SA_KEY     = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
OUT_BASE   = Path("output")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


# ── Normalisasi teks PDF ──────────────────────────────────────────────────────
def normalize_line(line: str) -> str:
    """
    Fix artefak umum PDF regulasi Indonesia:
    - 'Pasal2' → 'Pasal 2'
    - 'Pasa12' → 'Pasal 2'  (l terbaca sebagai 1)
    - 'BABI'   → 'BAB I'
    - 'ayat(1)' → 'ayat (1)'
    """
    # Fix 'Pasa1' atau 'Pasal' langsung diikuti angka (tanpa spasi)
    line = re.sub(r'(?i)\bPasa[l1]\s*(\d+[A-Z]?)\b', lambda m: f'Pasal {m.group(1)}', line)

    # Fix 'BAB' diikuti langsung angka romawi tanpa spasi: BABI, BABII, BABIII
    line = re.sub(r'(?i)\bBAB([IVXLCDM]+)\b', lambda m: f'BAB {m.group(1)}', line)

    # Fix ayat tanpa spasi: '(1)teks' → '(1) teks'
    line = re.sub(r'\((\d+)\)([^\s])', r'(\1) \2', line)

    # Fix huruf langsung diikuti teks tanpa spasi: 'a.teks' → 'a. teks'
    line = re.sub(r'^([a-z])\.([^\s])', r'\1. \2', line)

    return line.strip()


def is_page_number(line: str) -> bool:
    """Filter baris yang hanya berisi nomor halaman seperti '-4-' atau '4'."""
    return bool(re.match(r'^-\d+-$', line)) or (line.isdigit() and len(line) <= 3)


def is_noise(line: str) -> bool:
    """Filter header/footer berulang dan baris noise."""
    noise_patterns = [
        r'^MENTERI\s*PERHUBUNGAN$',
        r'^REPUBLIK\s*INDONESIA',
        r'^-\d+-$',           # nomor halaman -4-
        r'^\d+$',             # nomor halaman saja
        r'^ttd$',
        r'^PENILAIAN$',
        r'^ALASANUTAMA$',     # header tabel lampiran
        r'^No\.\s+ITEMUJI',   # header tabel lampiran
        r'^MiD\s+MaD\s+DD',  # header kolom tabel
    ]
    for p in noise_patterns:
        if re.match(p, line, re.IGNORECASE):
            return True
    return False


# ── Regex struktur hukum (setelah normalisasi) ────────────────────────────────
# Setelah normalize_line, 'Pasal 2' sudah rapi — regex bisa strict lagi
RE_PASAL    = re.compile(r'^Pasal\s+(\d+[A-Z]?)\s*$', re.IGNORECASE)
# Pasal inline di baris yang sama dengan teks (Pasal 4\nUji Berkala...)
RE_PASAL_INLINE = re.compile(r'^Pasal\s+(\d+[A-Z]?)\b', re.IGNORECASE)
RE_AYAT     = re.compile(r'^\((\d+)\)\s*(.*)')
RE_HURUF    = re.compile(r'^([a-z])\.\s+(.*)')
RE_ANGKA    = re.compile(r'^(\d+)\.\s+(.*)')   # 1. 2. 3. dalam pasal definisi
RE_BAB      = re.compile(r'^BAB\s+([IVXLCDM]+)\s*$', re.IGNORECASE)
RE_BAGIAN   = re.compile(r'^Bagian\s+(.+)', re.IGNORECASE)
RE_PARAGRAF = re.compile(r'^Paragraf\s+(\d+)', re.IGNORECASE)
RE_DEFINISI = re.compile(r'^(?:\d+\.\s+)?([A-Z][^"]+?)\s+adalah\s+', re.IGNORECASE)


# ── Dataclasses ───────────────────────────────────────────────────────────────
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
    reg_type: str
    status: str
    pasal_list: list[Pasal] = field(default_factory=list)
    konsideran: list[str] = field(default_factory=list)


# ── Download dari Google Drive ────────────────────────────────────────────────
def build_drive_service():
    if not SA_KEY or not os.path.exists(SA_KEY):
        print(f"ERROR: credentials tidak ditemukan: {SA_KEY}")
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def download_pdf(service, file_id: str) -> tuple[bytes, str]:
    try:
        meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    except Exception as e:
        print(f"ERROR: Gagal ambil metadata: {e}")
        sys.exit(1)

    filename: str = meta.get("name", "document.pdf")
    mime: str = meta.get("mimeType", "")
    print(f"Download: {filename}")

    buf = io.BytesIO()
    try:
        if mime == "application/vnd.google-apps.document":
            req = service.files().export_media(fileId=file_id, mimeType="application/pdf")
        else:
            req = service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"  {int(status.progress() * 100)}%")
    except Exception as e:
        print(f"ERROR: Gagal download: {e}")
        sys.exit(1)

    return buf.getvalue(), filename


# ── Ekstraksi teks mentah ─────────────────────────────────────────────────────
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
        print(f"pdfplumber error: {e}, fallback PyMuPDF")
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")  # type: ignore[call-arg]
        for page in doc:
            blocks = page.get_text("blocks")  # type: ignore[arg-type]
            for block in sorted(blocks, key=lambda b: (round(b[1] / 10), b[0])):
                for line in block[4].split("\n"):
                    normalized = normalize_line(line)
                    if normalized and not is_noise(normalized):
                        lines.append(normalized)
        doc.close()

    return lines


# ── Parser struktur hukum ─────────────────────────────────────────────────────
def flush_pasal(current_pasal: Pasal | None, current_ayat: Ayat | None, reg: Regulasi) -> None:
    """Simpan pasal aktif ke reg.pasal_list."""
    if current_pasal is None:
        return
    if current_ayat:
        current_pasal.ayat.append(current_ayat)
    reg.pasal_list.append(current_pasal)


def parse_regulasi(lines: list[str], reg: Regulasi) -> None:
    current_pasal: Pasal | None = None
    current_ayat: Ayat | None = None
    current_bab = ""
    current_bagian = ""
    in_konsideran = True

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── BAB ──
        if RE_BAB.match(line):
            current_bab = line
            # Judul BAB ada di baris berikutnya (KETENTUAN UMUM, dst)
            if i + 1 < len(lines) and not RE_PASAL_INLINE.match(lines[i + 1]):
                current_bab += f" {lines[i + 1]}"
                i += 1
            i += 1
            continue

        # ── BAGIAN ──
        if RE_BAGIAN.match(line):
            current_bagian = line
            i += 1
            continue

        # ── PARAGRAF ──
        if RE_PARAGRAF.match(line):
            current_bagian = line
            i += 1
            continue

        # ── PASAL ──
        m_pasal = RE_PASAL_INLINE.match(line)
        if m_pasal:
            in_konsideran = False
            flush_pasal(current_pasal, current_ayat, reg)
            current_ayat = None
            current_pasal = Pasal(
                nomor=m_pasal.group(1),
                bab=current_bab,
                bagian=current_bagian,
            )
            # Sisa teks setelah "Pasal N" di baris yang sama
            sisa = line[m_pasal.end():].strip()
            if sisa:
                current_pasal.teks_langsung.append(sisa)
            i += 1
            continue

        # Konsideran: teks sebelum Pasal 1
        if in_konsideran:
            reg.konsideran.append(line)
            i += 1
            continue

        if current_pasal is None:
            i += 1
            continue

        # ── AYAT: (1), (2), ... ──
        m_ayat = RE_AYAT.match(line)
        if m_ayat:
            if current_ayat:
                current_pasal.ayat.append(current_ayat)
            current_ayat = Ayat(nomor=m_ayat.group(1), teks=m_ayat.group(2).strip())
            i += 1
            continue

        # ── HURUF: a., b., ... ──
        m_huruf = RE_HURUF.match(line)
        if m_huruf:
            huruf_obj = Huruf(kode=m_huruf.group(1), teks=m_huruf.group(2).strip())
            if current_ayat:
                current_ayat.huruf.append(huruf_obj)
            else:
                # Huruf langsung di bawah pasal tanpa ayat
                current_pasal.teks_langsung.append(f"{huruf_obj.kode}. {huruf_obj.teks}")
            i += 1
            continue

        # ── ANGKA: 1., 2., ... (list definisi Pasal 1) ──
        m_angka = RE_ANGKA.match(line)
        if m_angka:
            if current_ayat:
                current_ayat.teks += f" {line}"
            else:
                current_pasal.teks_langsung.append(line)
            i += 1
            continue

        # ── Teks lanjutan ──
        if current_ayat:
            current_ayat.teks += f" {line}"
        else:
            current_pasal.teks_langsung.append(line)

        i += 1

    # Simpan pasal terakhir
    flush_pasal(current_pasal, current_ayat, reg)


# ── Generate MD ───────────────────────────────────────────────────────────────
def render_bunyi_pasal(pasal: Pasal) -> str:
    parts: list[str] = []

    if pasal.teks_langsung:
        parts.append(" ".join(pasal.teks_langsung))

    for ayat in pasal.ayat:
        if ayat.teks:
            parts.append(f"**({ayat.nomor})** {ayat.teks}")
        for huruf in ayat.huruf:
            parts.append(f"&nbsp;&nbsp;&nbsp;&nbsp;**{huruf.kode}.** {huruf.teks}")
        parts.append("")

    # Wrap semua dalam blockquote
    raw = "\n".join(parts).strip()
    return "\n".join(f"> {baris}" if baris else ">" for baris in raw.split("\n"))


def generate_pasal_md(pasal: Pasal, reg: Regulasi) -> str:
    bab_line = f'bab: "{pasal.bab}"' if pasal.bab else ""
    bagian_line = f'bagian: "{pasal.bagian}"' if pasal.bagian else ""
    fm_extras = "\n".join(filter(None, [bab_line, bagian_line]))

    frontmatter = f"""---
type: regulation
regulasi: {reg.reg_id}
pasal: {pasal.nomor}
kategori: {reg.reg_type}
status: {reg.status}
{fm_extras}
---"""

    heading = f"# {reg.title} Pasal {pasal.nomor}"
    bunyi   = f"## Bunyi Pasal\n\n{render_bunyi_pasal(pasal)}"
    catatan = "## Catatan\n\nBelum ada."
    nav     = "---\n[[index|← Daftar Pasal]]"

    return "\n\n".join([frontmatter, heading, bunyi, catatan, nav])


def generate_index_md(reg: Regulasi) -> str:
    lines = [
        f"""---
type: regulation-index
regulasi: {reg.reg_id}
kategori: {reg.reg_type}
status: {reg.status}
total_pasal: {len(reg.pasal_list)}
---""",
        f"# {reg.title}",
        f"**Kategori:** {reg.reg_type}  \n**Status:** {reg.status}  \n**Total Pasal:** {len(reg.pasal_list)}",
        "## Daftar Pasal",
        "",
    ]

    current_bab = ""
    for pasal in reg.pasal_list:
        if pasal.bab and pasal.bab != current_bab:
            current_bab = pasal.bab
            lines.append(f"\n### {current_bab}\n")
        slug = f"pasal-{pasal.nomor.lower()}"
        lines.append(f"- [[{slug}|Pasal {pasal.nomor}]]")

    return "\n".join(lines)


# ── Debug helper ──────────────────────────────────────────────────────────────
def write_debug(out_dir: Path, lines: list[str], reg: Regulasi) -> None:
    # Raw lines setelah normalisasi
    (out_dir / "_debug_raw_lines.txt").write_text("\n".join(lines), encoding="utf-8")

    # Pasal yang terdeteksi
    detected = sorted(
        [int(re.sub(r"[^0-9]", "", p.nomor)) for p in reg.pasal_list if p.nomor.isdigit() or re.sub(r"[^0-9]", "", p.nomor)],
    )
    missing = [i for i in range(detected[0], detected[-1] + 1) if i not in detected] if detected else []

    debug_info = [
        f"Terdeteksi: {len(reg.pasal_list)} pasal",
        f"Range: Pasal {detected[0]} - Pasal {detected[-1]}" if detected else "",
        f"Missing ({len(missing)}): {missing}",
        "",
        "=== DETAIL PASAL ===",
    ]
    for p in reg.pasal_list:
        ayat_count = len(p.ayat)
        huruf_count = sum(len(a.huruf) for a in p.ayat)
        debug_info.append(f"Pasal {p.nomor:>4} | {ayat_count} ayat | {huruf_count} huruf | bab: {p.bab[:30] if p.bab else '-'}")

    (out_dir / "_debug_summary.txt").write_text("\n".join(debug_info), encoding="utf-8")
    print(f"  Missing pasal: {missing}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not FILE_ID:
        print("ERROR: GDRIVE_FILE_ID tidak diset")
        sys.exit(1)
    if not REG_ID:
        print("ERROR: REG_ID tidak diset")
        sys.exit(1)

    service = build_drive_service()
    pdf_bytes, original_name = download_pdf(service, FILE_ID)

    print("Mengekstrak teks...")
    lines = extract_raw_lines(pdf_bytes)
    print(f"  {len(lines)} baris terdeteksi")

    reg = Regulasi(
        reg_id=REG_ID,
        title=REG_TITLE or REG_ID.upper(),
        reg_type=REG_TYPE or "regulasi",
        status=REG_STATUS,
    )
    parse_regulasi(lines, reg)
    print(f"  {len(reg.pasal_list)} pasal terdeteksi")

    if not reg.pasal_list:
        print("ERROR: Tidak ada pasal terdeteksi.")
        sys.exit(1)

    out_dir = OUT_BASE / REG_ID
    out_dir.mkdir(parents=True, exist_ok=True)

    write_debug(out_dir, lines, reg)

    for pasal in reg.pasal_list:
        slug = f"pasal-{pasal.nomor.lower()}"
        md_content = generate_pasal_md(pasal, reg)
        (out_dir / f"{slug}.md").write_text(md_content, encoding="utf-8")

    index_content = generate_index_md(reg)
    (out_dir / "index.md").write_text(index_content, encoding="utf-8")

    print(f"\n✓ Selesai: {len(reg.pasal_list)} pasal + index → output/{REG_ID}/")


if __name__ == "__main__":
    main()