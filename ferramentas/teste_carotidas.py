"""Teste da escolha de PS/ED na caixa das carotidas (valores sinteticos, sem paciente).
Rodar: python ferramentas/teste_carotidas.py  (da pasta do repo)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "EcoPDF"))
import ocr_imagem as O  # noqa: E402


def caso(linhas, palavra="ACID"):
    return {k: v for k, v, _, _ in O._carotida(None, [(t, 0.1 if d is None else d) for t, d in linhas], [palavra])}


# remarcacao embaixo com valor MAIOR (como nos exames reais do S6)
assert caso([("# PS 69.28 cm/s", None), ("ED 0.00 cm/s", None),
             ("# Rt Prox ACC ED 21.38 cm/s", None), ("# Rt Prox ACC PS 122.64 cm/s", None)]) == \
    {"aci_vps_d": 122.64, "aci_vdf_d": 21.38}
# remarcacao embaixo com valor MENOR: antes saia 122.64 (o maior)
assert caso([("# PS 122.64 cm/s", None), ("ED 0.00 cm/s", None),
             ("# Rt Prox ACC ED 21.38 cm/s", None), ("# Rt Prox ACC PS 69.28 cm/s", None)]) == \
    {"aci_vps_d": 69.28, "aci_vdf_d": 21.38}
# T8 com numero lido: #1 em cima, remarcacao #3 ED e #2 PS embaixo
assert caso([("#1 Rt Prox ACC PS 79.65 cm/s", None), ("Rt Prox ACC ED 0.00 cm/s", None),
             ("#3 Rt Prox ACC ED 28.86 cm/s", None), ("#2 Rt Prox ACC PS 85.84 cm/s", None)], "ACCD") == \
    {"acc_d": 85.84}
# linha PS de baixo mal lida: PS fica em branco (nao volta para a de cima)
assert caso([("# PS 122.64 cm/s", None), ("ED 0.00 cm/s", None),
             ("# Rt Prox ACC ED 21.38 cm/s", None), ("# Rt Prox ACC PS 6?.28 cm/s", 3.0)]) == \
    {"aci_vdf_d": 21.38}
# uma medida so
assert caso([("# PS 84.39 cm/s", None), ("ED 26.45 cm/s", None)]) == {"aci_vps_d": 84.39, "aci_vdf_d": 26.45}
print("OK: 5 casos")
