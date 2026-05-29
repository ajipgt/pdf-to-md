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
FILE_ID   = os.environ.get("GDRIVE_FILE_ID", "").strip()
REG_ID    = os.environ.get("REG_ID", "").strip()       # misal: pp-55-2012
REG_TITLE = os.environ.get("REG_TITLE", "").strip()    # misal: PP 55 Tahun 2012
REG_TYPE  = os.environ.get("REG_TYPE", "").strip()     # misal: peraturan-pemerintah
REG_STATUS = os.environ.get("REG_STATUS", "berlaku").strip()
SA_KEY    = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
OUT_BASE  = Path("output")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ── Pattern regex struktur hukum Indonesia ───────────────────────────────────
RE_PASAL = re.compile(
    r"^Pasal\s+(\d+[A-Z]?)\b",
    re.IGNORECASE,
)

RE_AYAT = re.compile(
    r"^\((\d+)\)\s*(.*)"
)

RE_HURUF = re.compile(
    r"^([a-z])\.\s*(.*)"
)
RE_BAB      = re.compile(r'^BAB\s+([IVXLCDM]+)\s*$')
RE_BAGIAN   = re.compile(r'^Bagian\s+(.+)', re.IGNORECASE)
RE_PARAGRAF = re.compile(r'^Paragraf\s+(\d+)', re.IGNORECASE)
# Deteksi definisi: "... adalah ..." atau "... merupakan ..."


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
    nomor: str          # "1", "2A", dst
    bab: str = ""
    bagian: str = ""
    ayat: list[Ayat] = field(default_factory=list)
    teks_langsung: list[str] = field(default_factory=list)  # pasal tanpa ayat


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
    creds = service_account.Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def download_pdf(service, file_id: str) -> tuple[bytes, str]:
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    filename: str = meta.get("name", "document.pdf")
    mime: str = meta.get("mimeType", "")
    print(f"Download: {filename}")

    buf = io.BytesIO()
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

    return buf.getvalue(), filename


# ── Ekstraksi teks mentah dari PDF ───────────────────────────────────────────
def extract_raw_lines(pdf_bytes: bytes) -> list[str]:
    """Ekstrak semua baris teks dari PDF, bersih dari artefak."""
    lines: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                for line in text.split("\n"):
                    clean = line.strip()
                    if clean:
                        lines.append(clean)
    except Exception as e:
        print(f"pdfplumber error: {e}, fallback PyMuPDF")
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")  # type: ignore[call-arg]
        for page in doc:
            blocks = page.get_text("blocks")  # type: ignore[arg-type]
            for block in sorted(blocks, key=lambda b: (round(b[1] / 10), b[0])):
                for line in block[4].split("\n"):
                    clean = line.strip()
                    if clean:
                        lines.append(clean)
        doc.close()

    return lines


# ── Parser struktur hukum ─────────────────────────────────────────────────────
def parse_regulasi(lines: list[str], reg: Regulasi) -> None:
    """Parse baris teks mentah → struktur Pasal/Ayat/Huruf."""
    current_pasal: Pasal | None = None
    current_ayat: Ayat | None = None
    current_bab = ""
    current_bagian = ""
    in_konsideran = True  # teks sebelum Pasal 1 = konsideran

    i = 0
    while i < len(lines):
        line = lines[i]

        # Deteksi BAB
        if RE_BAB.match(line):
            current_bab = line
            # Baris berikutnya biasanya judul BAB
            if i + 1 < len(lines) and not RE_PASAL.match(lines[i + 1]):
                current_bab += f" {lines[i + 1]}"
                i += 1
            i += 1
            continue

        # Deteksi Bagian
        m_bagian = RE_BAGIAN.match(line)
        if m_bagian:
            current_bagian = line
            i += 1
            continue

        # Deteksi Paragraf (reset bagian)
        if RE_PARAGRAF.match(line):
            current_bagian = line
            i += 1
            continue

        # Deteksi PASAL
        m_pasal = RE_PASAL.match(line)
        if m_pasal:
            in_konsideran = False
            if current_pasal:
                # Simpan ayat terakhir
                if current_ayat:
                    current_pasal.ayat.append(current_ayat)
                    current_ayat = None
                reg.pasal_list.append(current_pasal)

            current_pasal = Pasal(
                nomor=m_pasal.group(1),
                bab=current_bab,
                bagian=current_bagian,
            )
            i += 1
            continue

        # Konsideran (sebelum Pasal 1)
        if in_konsideran:
            reg.konsideran.append(line)
            i += 1
            continue

        if current_pasal is None:
            i += 1
            continue

        # Deteksi AYAT: (1), (2), ...
        m_ayat = RE_AYAT.match(line)
        if m_ayat:
            if current_ayat:
                current_pasal.ayat.append(current_ayat)
            current_ayat = Ayat(nomor=m_ayat.group(1), teks=m_ayat.group(2))
            i += 1
            continue

        # Deteksi HURUF: a., b., ...
        m_huruf = RE_HURUF.match(line)
        if m_huruf:
            target = current_ayat if current_ayat else None
            if target:
                target.huruf.append((m_huruf.group(1), m_huruf.group(2)))
            i += 1
            continue

        # Teks biasa — lanjutan ayat atau teks langsung pasal
        if current_ayat:
            # Cek apakah ini lanjutan teks ayat (bukan pasal/ayat baru)
            current_ayat.teks += f" {line}"
        else:
            current_pasal.teks_langsung.append(line)

        i += 1

    # Simpan pasal terakhir
    if current_pasal:
        if current_ayat:
            current_pasal.ayat.append(current_ayat)
        reg.pasal_list.append(current_pasal)




# ── Generate Markdown per pasal ───────────────────────────────────────────────
def render_bunyi_pasal(
    pasal: Pasal
) -> str:

    parts: list[str] = []

    if pasal.teks_langsung:
        parts.append(
            " ".join(
                pasal.teks_langsung
            )
        )

    for ayat in pasal.ayat:

        if ayat.teks:
            parts.append(
                f"({ayat.nomor}) {ayat.teks}"
            )

        for huruf in ayat.huruf:
            parts.append(
                f"{huruf.kode}. {huruf.teks}"
            )

        parts.append("")

    return "\n".join(parts).strip()

def generate_pasal_md(pasal: Pasal, reg: Regulasi, semua_konsep: list[str]) -> str:
    """Generate satu file MD untuk satu pasal."""
    slug_pasal = f"pasal-{pasal.nomor.lower()}"
    tag_bab = f"bab-{pasal.bab.split()[1].lower()}" if pasal.bab else ""

    

    frontmatter = f"""---
type: regulation
regulasi: {reg.reg_id}
pasal: {pasal.nomor}
kategori: {reg.reg_type}
status: {reg.status}
{f'bab: "{pasal.bab}"' if pasal.bab else ''}
{f'bagian: "{pasal.bagian}"' if pasal.bagian else ''}
---"""

    heading = f"# {reg.title} Pasal {pasal.nomor}"

    bunyi = f"## Bunyi Pasal\n\n{render_bunyi_pasal(pasal, semua_konsep)}"

    
    nav_parts = [f"[[index|← Daftar Pasal]]"]
    catatan_nav = "\n\n---\n" + " · ".join(nav_parts)

    return "\n\n".join([frontmatter, heading, Bunyi, ]) + catatan_nav


def generate_index_md(reg: Regulasi) -> str:
    """Generate index.md berisi daftar semua pasal + metadata regulasi."""
    lines = [
        f"""---
type: regulation-index
regulasi: {reg.reg_id}
kategori: {reg.reg_type}
status: {reg.status}
total_pasal: {len(reg.pasal_list)}
---""",
        f"# {reg.title}",
        f"**Kategori:** {reg.reg_type}  ",
        f"**Status:** {reg.status}  ",
        f"**Total Pasal:** {len(reg.pasal_list)}",
        "",
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


# ── Main pipeline ─────────────────────────────────────────────────────────────
def main() -> None:
    if not FILE_ID:
        print("ERROR: GDRIVE_FILE_ID tidak diset")
        sys.exit(1)
    if not REG_ID:
        print("ERROR: REG_ID tidak diset (contoh: pp-55-2012)")
        sys.exit(1)

    # Download PDF
    service = build_drive_service()
    pdf_bytes, original_name = download_pdf(service, FILE_ID)

    # Ekstrak teks mentah
    print("Mengekstrak teks...")
    lines = extract_raw_lines(pdf_bytes)
    out_dir = OUT_BASE / REG_ID
out_dir.mkdir(
    parents=True,
    exist_ok=True,
)

(out_dir / "_debug_raw_lines.txt").write_text(
    "\n".join(lines),
    encoding="utf-8",
)
    print(f"  {len(lines)} baris terdeteksi")

    # Parse struktur
    reg = Regulasi(
        reg_id=REG_ID,
        title=REG_TITLE or REG_ID.upper(),
        reg_type=REG_TYPE or "regulasi",
        status=REG_STATUS,
    )
    parse_regulasi(lines, reg)
    nomor_pasal: list[int] = []

for p in reg.pasal_list:
    try:
        nomor_pasal.append(
            int(
                re.sub(
                    r"[^0-9]",
                    "",
                    p.nomor,
                )
            )
        )
    except ValueError:
        pass

nomor_pasal.sort()

missing: list[int] = []

if nomor_pasal:
    for i in range(
        nomor_pasal[0],
        nomor_pasal[-1],
    ):
        if i not in nomor_pasal:
            missing.append(i)

(out_dir / "_debug_missing_pasal.txt").write_text(
    "\n".join(
        f"Pasal {x}"
        for x in missing
    ),
    encoding="utf-8",
)
    print(f"  {len(reg.pasal_list)} pasal terdeteksi")

    if not reg.pasal_list:
        print("ERROR: Tidak ada pasal terdeteksi. Cek format PDF.")
        sys.exit(1)

    

    # Buat folder output
    out_dir = OUT_BASE / REG_ID
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate file per pasal
    for pasal in reg.pasal_list:
        slug = f"pasal-{pasal.nomor.lower()}"
        md_content = generate_pasal_md(pasal, reg,)
        (out_dir / f"{slug}.md").write_text(md_content, encoding="utf-8")

    # Generate index
    index_content = generate_index_md(reg)
    (out_dir / "index.md").write_text(index_content, encoding="utf-8")

    print(f"\n✓ Selesai: {len(reg.pasal_list)} pasal + index → output/{REG_ID}/")


if __name__ == "__main__":
    main()