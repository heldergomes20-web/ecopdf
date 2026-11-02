"""Gera EcoPDF.zip a partir da pasta EcoPDF/ do repositorio (o mesmo zip que vai no release).

Uso: python ferramentas/empacotar.py [destino.zip]
Confere que EcoPDF/VERSAO.txt tem formato valido e, se a variavel TAG existir,
que ela e igual a versao (o workflow de release usa isso).
"""
import os
import re
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FONTE = RAIZ / "EcoPDF"
IGNORAR = {"__pycache__", "venv", "log", "processados", "dicom", "_anterior"}
DATA_FIXA = (2020, 1, 1, 0, 0, 0)  # zip igual para o mesmo conteudo


def main():
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ / "EcoPDF.zip"
    versao = (FONTE / "VERSAO.txt").read_text(encoding="utf-8-sig").strip()
    if not re.match(r"^\d{4}\.\d{2}\.\d{2}(\.\d+)?$", versao):
        sys.exit(f"VERSAO.txt invalido: {versao!r}")
    tag = os.environ.get("TAG")
    if tag and tag != versao:
        sys.exit(f"A tag {tag} nao bate com EcoPDF/VERSAO.txt ({versao})")
    arquivos = sorted(p for p in FONTE.rglob("*")
                      if p.is_file() and not IGNORAR & set(p.relative_to(FONTE).parts))
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("EcoPDF/", DATA_FIXA), "")
        for p in arquivos:
            info = zipfile.ZipInfo("EcoPDF/" + p.relative_to(FONTE).as_posix(), DATA_FIXA)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    print(f"{destino} ({versao}, {len(arquivos)} arquivos)")


if __name__ == "__main__":
    main()
