import os
import re
from pathlib import Path

OUT_BASE = Path("output")
REG_ID   = os.environ.get("REG_ID", "").strip()

# ── Pattern cleanup ───────────────────────────────────────────────────────────

# Deteksi huruf inline di dalam blockquote teks ayat
# "> **(1)** dilakukan dengan: a. hal pertama b. hal kedua"
RE_HURUF_INLINE = re.compile(r'\s+([a-z])\.\s+(?=[a-z])')

# Baris blockquote ayat: "> **(N)** teks..."
RE_BLOCKQUOTE_AYAT = re.compile(r'^(>\s+\*\*\(\d+\)\*\*\s+.+)$')

# Deteksi huruf yang sudah jadi nested blockquote (sudah benar, skip)
RE_HURUF_NESTED = re.compile(r'^>\s+>\s+\*\*[a-z]\.\*\*')


def split_huruf_dari_teks(teks_ayat: str) -> list[str]:
    """
    Split teks ayat yang mengandung huruf inline jadi baris-baris terpisah.

    Input:  '> **(2)** dilakukan dengan: a. paling lama 13 hari b. paling lama 13 hari'
    Output: [
        '> **(2)** dilakukan dengan:',
        '>> **a.** paling lama 13 hari',
        '>> **b.** paling lama 13 hari',
    ]
    """
    # Strip prefix '> '
    prefix = '> '
    if not teks_ayat.startswith(prefix):
        return [teks_ayat]

    konten = teks_ayat[len(prefix):]

    # Split berdasarkan pola ' X. ' dimana X huruf kecil tunggal
    pattern = re.compile(r'\s+([a-z])\.\s+')
    parts = pattern.split(konten)

    if len(parts) <= 1:
        return [teks_ayat]  # tidak ada huruf inline, kembalikan asli

    teks_utama = parts[0].rstrip(': ').rstrip()
    hasil = [f'> {teks_utama}:' if not teks_utama.endswith(':') else f'> {teks_utama}']

    i = 1
    while i + 1 < len(parts):
        kode = parts[i]
        isi  = parts[i + 1].strip()
        if len(kode) == 1 and kode.isalpha():
            hasil.append(f'>> **{kode}.** {isi}')
        i += 2

    return hasil


def cleanup_md_file(path: Path) -> bool:
    """
    Baca file MD, rapikan formatting, tulis kembali.
    Return True jika ada perubahan.
    """
    original = path.read_text(encoding='utf-8')
    lines    = original.splitlines()
    result   = []
    changed  = False

    for line in lines:
        # Hanya proses baris blockquote yang mengandung ayat
        if RE_BLOCKQUOTE_AYAT.match(line) and not RE_HURUF_NESTED.match(line):
            # Cek apakah ada huruf inline
            if RE_HURUF_INLINE.search(line):
                expanded = split_huruf_dari_teks(line)
                if len(expanded) > 1:
                    result.extend(expanded)
                    changed = True
                    continue

        result.append(line)

    if changed:
        path.write_text('\n'.join(result) + '\n', encoding='utf-8')

    return changed


def main() -> None:
    # Tentukan folder target
    if REG_ID:
        targets = [OUT_BASE / REG_ID]
    else:
        targets = [d for d in OUT_BASE.iterdir() if d.is_dir()]

    total_files   = 0
    total_changed = 0

    for folder in targets:
        if not folder.exists():
            print(f"Folder tidak ditemukan: {folder}")
            continue

        md_files = sorted(folder.glob('pasal-*.md'))
        print(f"\n{folder.name}: {len(md_files)} file")

        for md_file in md_files:
            changed = cleanup_md_file(md_file)
            total_files += 1
            if changed:
                total_changed += 1
                print(f"  ✓ Diperbaiki: {md_file.name}")

    print(f"\nSelesai: {total_changed}/{total_files} file diubah")


if __name__ == '__main__':
    main()