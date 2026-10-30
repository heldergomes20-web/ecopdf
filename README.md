# EcoPDF

Uso interno da Clínica Ecocardio (João Pessoa/PB). Recebe as imagens do aparelho de eco por DICOM, monta a folha de imagens e junta ao laudo em PDF.

Código público só para que os computadores das salas consigam baixar as atualizações. Todos os direitos reservados (veja LICENSE).

## Pastas

| Pasta | Conteúdo |
| --- | --- |
| `EcoPDF/` | O programa, exatamente como vai para `C:\EcoPDF` em cada sala |
| `ferramentas/empacotar.py` | Gera o `EcoPDF.zip` a partir de `EcoPDF/` |
| `ferramentas/assinador/` | Assinador (vai para o pendrive; a chave privada nunca entra aqui) |
| `docs/` | Regras do atualizador e histórico de versões |

## Como sai uma versão

1. `EcoPDF/VERSAO.txt` e `EcoPDF/NOVIDADES.txt` atualizados (número AAAA.MM.DD.N).
2. Tag com o mesmo número: o GitHub Actions cria um **rascunho** de release com o `EcoPDF.zip`.
3. O responsável assina o zip offline e anexa `EcoPDF.zip.sig`.
4. Publica como **pre-release** (máquina de teste) e depois marca como **Latest** (todas as salas).

As salas só instalam versões com assinatura válida e número maior que o instalado.
