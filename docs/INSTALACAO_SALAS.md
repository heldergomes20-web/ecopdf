# Instalação das salas: Bessa como referência e método para as próximas

Estado em 04/11/2026. Duas partes:

1. **Bessa como está funcionando** (instalado em 29/10, reconfigurado em 03/11). Serve de referência para corrigir problemas.
2. **Método para as próximas salas**: o que se repete igual, o que muda por aparelho e o que ainda não se sabe.

Uma fonte por fato. Este arquivo guarda o desenho da instalação e o método. A situação de cada sala (versão instalada, pendências do dia a dia, nome do PC, MAC) fica no **Registro das máquinas** (Claude Doc). O passo a passo para quem instala fica no **Manual de instalação** (Claude Doc). O histórico das versões fica em `docs/HISTORICO_VERSOES.md`. Dados de paciente e segredos (senha do Medware, chave da planilha) não entram aqui, porque o repositório é público.

---

## Parte 1. Bessa (GE Vivid S6)

### 1.1 Visão geral do caminho dos dados

```
Medware (Firebird, mesmo PC) ──leitura──► EcoPDF (PC do laudo, 192.168.10.172)
                                             │  porta 11112, AE ECOPDF
Vivid S6 ──Query (worklist C-FIND)──────────►│  responde a agenda do dia
Vivid S6 ──Store (C-STORE: fotos + SR)──────►│  monta a folha de imagens
Medware ──imprime na PDF24──► pasta 0 ──────►│  renomeia o laudo, junta laudo + folha
                                             ├─► pasta 3 (final) ─► subido à mão no Amigo
                                             ├─► impressão direta: fotos na "EcoPDF Foto", depois laudo na L3250
                                             └─► painel: status para a planilha a cada 15 min (desde a .4)
```

### 1.2 PC do laudo

| Item | Valor |
| --- | --- |
| Sistema | Windows 11, usuário W11, Python 3.14 (`py`), EcoPDF num venv próprio em `C:\EcoPDF\venv` |
| IP | **192.168.10.172, fixo no próprio Windows** (DHCP desligado, 03/11). Não há reserva no roteador |
| Rede a ignorar | Adaptador "Topaz Loopback" (não é a rede da clínica; não mexer) |
| Nome do PC e MAC | no Registro das máquinas (identificam a sala na aba PCs da planilha) |
| Início automático | Agendador de Tarefas, tarefa `EcoPDF` (`venv\Scripts\pythonw.exe ecopdf.py`) |
| Firewall | Regra `EcoPDF DICOM`: entrada TCP 11112, só sub-rede local (criada pelo `instalar.bat`). Nenhum outro bloqueio foi preciso |
| Pastas | `Área de Trabalho\Laudos - Não Apagar` (oculta): 0 - Entrada PDF24, 1 - Laudos aguardando imagens, 2 - Imagens aguardando laudo, 3 - Finalizados (subir no prontuário), 9 - Revisar. Atalho "Laudos prontos" na Área de Trabalho e pasta 3 no Acesso rápido |
| PDF24 | Salvamento automático em `0 - Entrada PDF24` |
| Tela | Janela própria do Edge "EcoPDF – Exames", `http://127.0.0.1:8765`. Abre quando chega imagem ou laudo; se já está aberta (mesmo minimizada), é trazida para a frente (desde a .3, busca pelo título da janela) |
| Arquivos locais fora do zip | `C:\EcoPDF\painel.ini` (PAINEL_URL, PAINEL_CHAVE, MEDWARE_SENHA; copiado do pendrive) e `C:\EcoPDF\canal.ini` (canal de atualização) |

### 1.3 Versão e atualização

| Item | Valor |
| --- | --- |
| Versão instalada | **2026.11.03.3** (instalada 03/11). A 2026.11.03.4 (painel a cada 15 min) é a Latest no GitHub desde 03/11 17:47 e entra no Bessa quando alguém clicar no número da versão na tela |
| Canal | **geral** desde 03/11 (`atualizar_agora.bat geral`). Os colegas passam a usar o Bessa; ele deixou de ser o PC piloto |
| Próximo piloto | O PC onde o Helder for testar recebe `atualizar_agora.bat teste` |
| Como atualizar | Clicar no número da versão na tela → "Atualizar EcoPDF". Ou `C:\EcoPDF\atualizar_agora.bat`. Volta: `voltar.bat` (e volta automática se a porta DICOM não abrir em 60 s) |
| Atualização automática | A partir da 2026.11.04: tarefas `EcoPDF Atualizar 6h` e `EcoPDF Atualizar ao ligar` (criadas pelo instalar.bat). Desliga no PC com `AUTO = nao` no canal.ini |

### 1.4 Vivid S6

| Item | Configuração |
| --- | --- |
| Sondas | M4S-RS (eco). Linear para carótidas (nome não chega no DICOM) |
| Identificação | O S6 não manda DeviceSerialNumber nem TransducerData. StationName = `VIVID_S6-008046` → marca **8046** na barra "Página" da folha (desde a 2026.11.03.1) |
| Envio de rotina | Tecla **Store**. Config → Conectividade → Dataflow: **"Worklist/Local Archive - DICOM Server/Int. HD" é o Padrão**. Inputs Worklist + Database; outputs Database + Dicom storage |
| Destino ECOPDF | (ECOPDF) 192.168.10.172, AE ECOPDF, porta 11112, Jpeg 95, Allow Multiframe **desmarcado**, Raw Data desmarcado, SR marcado |
| Worklist | No mesmo fluxo: (ECOPDF) 192.168.10.172, AE ECOPDF, porta 11112. Antes apontava para um MEDWARE antigo (AE MEDWARE, porta 104), tentativa velha de comDICOM; foi trocado |
| P1 | Só "Store to clipboard". Dicom storage tirado do P1 para não duplicar |
| Reenvio de exame antigo | Exportar → Para: ECOPDF. Usa o fluxo **"DICOM Storage"**, cujo Dicom storage foi apontado para (ECOPDF) 192.168.10.172:11112, sem multiframe, sem raw data, SR sim |
| Destinos apagados | SERVIDOR (192.168.55.24) e SERVIDOR2 (192.168.55.25), servidores antigos de outra rede. As tarefas presas para eles travavam o Spooler |
| Destino antigo mantido | MEDWARE (192.168.10.172, porta 105) na lista de dispositivos, sem uso |
| Cadastro do paciente | Query na tela de paciente puxa a agenda do dia. Sem worklist: Criar paciente à mão (continua funcionando) |
| Fotos | Só se grava a foto que vai para o laudo. Loops que chegarem são descartados no PC (QUADRO_LOOP = ignorar) |
| Spooler | Atalho: segurar a tecla indicada no teclado + ALT + S ("DICOM job spooler"). Não abriu no dia; o Vivid foi reiniciado em 03/11 ~15h54 |

**Pegadinhas do Vivid vistas no Bessa**

- Depois de mudar porta ou IP no Config, a tela de paciente continua com a configuração antiga até trocar o Dataflow nessa tela e voltar.
- A "carinha" (Check/Echo) do destino é o teste de rede. Echo ok e nada chegando = problema no próprio aparelho (fila), não na rede.
- Query dá erro rápido quando não há servidor de worklist respondendo. Não trava o exame.
- Fila do Spooler: tarefas para destino morto esperam ~40 s cada e ficam na frente das novas. O reenvio pelo Exportar ficou preso atrás de 42 a 63 tarefas.
- O nome do paciente vem inteiro no campo Sobrenome (sem `^`). É proposital.
- Medidas de usuário do pacote (E′SEP, E′LAT) não vão no SR. O EcoPDF lê esses valores da caixa de medida na imagem (OCR com `fonte_vivid.json`).
- Carótidas: o SR vascular grava todos os vasos como "Proximal CCA Right". As velocidades vêm da imagem: nome do vaso digitado em amarelo (ACCD/ACCE/ACID/ACIE/ACED/ACEE/AVD/AVE) + caixa PS/ED. Nome por extenso ("art. vert. dir.") ainda não é lido.

### 1.5 Medware e lista de pacientes (worklist)

| Item | Valor |
| --- | --- |
| Programa | Medware LaudosFlex (.NET), `C:\Medware LaudosFlex` |
| Banco | Firebird 2.5.8 Classic 32-bit, serviço `FirebirdServerDefaultInstance` (automático), **no próprio PC do laudo**. Arquivo `C:\Medware LaudosFlex\BD\Clinicas.fdb` |
| Acesso | `firebirdsql` (Python puro) por TCP 3050, usuário SYSDBA, só leitura. Senha em `painel.ini` (MEDWARE_SENHA) |
| Configuração | `WORKLIST = sim`, `MEDWARE_SERVIDORES = 127.0.0.1` (padrão do config.ini) |
| Conteúdo da lista | Agenda do dia (AGENDAMENTO + PACIENTE + MEDICO + PROCEDIMENTO, sem cancelado/falta/suspenso), uma linha por paciente/dia. Nome ASCII maiúsculo, ID = CODPACIENTE, nascimento, sexo, peso kg, altura (cm → m), médico executante. Descrição ECO / DCV / ECO FETAL ("ECO + DCV") |
| Saída da lista | Quando chegam imagens de todas as siglas do dia. Botão Worklist na tela: Excluir / Voltar à lista (vale só no dia) |
| Seletor de médico | "Worklist [Todos os médicos ▾]" no topo da tela. Filtra o que este aparelho recebe; volta a Todos à meia-noite ou com a tela 3 min sem contato. Depois de trocar, dar Query de novo no aparelho |
| Conferido no aparelho | Nome, ID, nascimento, sexo, altura, peso, ASC calculada, Méd. Diagn. e descrição preenchidos (agenda real, 03/11) |
| Tipo de exame | O S6 não manda a sonda. Separação pela frequência: ≥ 6 MHz = carótidas (linear 8,9 MHz), abaixo = eco (M4S 1,7 MHz) |

### 1.6 Impressão

| Item | Valor |
| --- | --- |
| Impressora | Epson EcoTank L3250, **USB**. Padrão do Windows = fila **"EPSON L3250 Series"**, porta **USB001** |
| Filas a não usar | EPSON4EEDCE e EPSON36EC7C (portas WSD, achadas sozinhas pelo Windows; duas impressoras de rede diferentes). Não podem virar padrão |
| Fila de fotos | **"EcoPDF Foto"**: mesmo driver e porta USB001, preferências Epson Premium Photo Paper Glossy + qualidade Alta. Criada pela instalação; refazer com `criar_fila_foto.bat` |
| Como imprime | Impressão direta (IMPRESSAO_DIRETA = sim): um envio com as fotos na "EcoPDF Foto" e, depois que ele sai da fila, um envio com o laudo na padrão. "Imprimir selecionados" junta as fotos de vários exames num envio e depois os laudos na mesma ordem. Papel foto por cima do comum, bandeja única |

### 1.7 Painel das salas

| Item | Valor |
| --- | --- |
| Planilha | "EcoPDF - Painel das salas" (Google, conta do Helder). Endereço e chave só no `painel.ini` |
| Identificação da sala | Nome do computador cadastrado na aba **PCs** da planilha → sala |
| Frequência | A cada 15 min a partir da 2026.11.03.4 (PAINEL_INTERVALO_MIN). Na .3, que está no Bessa, ainda 9h e 15h. "Sem sinal" segue pelos envios esperados das 9h/15h |
| Colunas usadas no Bessa | Fila EcoPDF Foto, Lista de pacientes (worklist), Medidas, última imagem/conexão, versão, erros |

### 1.8 Pendências do Bessa

- [ ] Spooler: confirmar se as tarefas presas sumiram depois do reinício do Vivid. Se voltarem, suporte da GE.
- [ ] Primeira impressão real com papel foto pela "EcoPDF Foto" (e a ordem em que a Epson empilha no "Imprimir selecionados").
- [ ] Atualizar para a 2026.11.03.4 e conferir na planilha, no dia seguinte, o envio a cada 15 min.
- [ ] Confirmar o nome da sonda linear (não vem no DICOM; hoje irrelevante por causa da frequência).
- [ ] Nome do vaso por extenso no OCR de carótidas (precisa de uma imagem de amostra).
- [ ] Atualizar os 5 documentos (guia dos médicos, fluxo, registro, atualizações, MANUAL_ATUALIZACAO) para a 2026.11.03.x.

---

## Parte 2. Método para as próximas salas

### 2.1 O que é igual em todas

- O mesmo `EcoPDF.zip` e o mesmo `config.ini` em todos os PCs. Nada é copiado PC a PC além dos dois arquivos de segredo/estado (`painel.ini` do pendrive; `canal.ini` só no PC piloto).
- Um EcoPDF independente por PC de laudo. Cada aparelho manda para um único PC, e o laudo daquele aparelho é feito nesse PC. Sem pasta compartilhada.
- AE ECOPDF, porta 11112, para imagens e worklist.
- Só fotos do laudo são gravadas; loops são descartados.
- PC novo começa no canal **geral**.

### 2.2 Antes de ir à sala

| O que | Por quê |
| --- | --- |
| Versão Latest assinada no GitHub e testada no piloto | A instalação pelo pendrive baixa a Latest |
| Pendrive "Instalador EcoPDF" + `painel.ini` | Instalar e ligar ao painel |
| Saber onde está o Medware da unidade | Define MEDWARE_SERVIDORES (ver 2.5) |
| Linha da sala na aba PCs da planilha (nome do PC entra no dia) | Painel identifica a sala |

### 2.3 Roteiro (na ordem)

1. **Fotos antes de mexer**: telas de rede/DICOM do aparelho e lista de destinos existentes.
2. **PC do laudo**
   1. IP do PC: fixo no Windows (como no Bessa) ou reserva no roteador. Anotar IP, máscara, gateway, MAC.
   2. Impressora da sala como **padrão** do Windows, na porta física (USB ou porta da marca). Rodar `Get-Printer | ft Name,DriverName,PortName -auto` e conferir que a padrão não é WSD nem PDF24.
   3. Pendrive → `INSTALAR ECOPDF.bat` → T. Copiar `painel.ini` para `C:\EcoPDF`. A instalação cria venv, pastas, firewall, tarefa agendada e a "EcoPDF Foto" (escolher papel foto + Alta). Guardar foto do `DADOS DESTE PC.txt`.
   4. PDF24: salvamento automático em `0 - Entrada PDF24` (caminho no DADOS DESTE PC.txt, a pasta é oculta).
   5. Medware: só se o banco não estiver neste PC, pôr `MEDWARE_SERVIDORES` e `MEDWARE_SENHA` no `painel.ini` e liberar a porta 3050 no firewall do servidor.
3. **Aparelho**
   1. Cadastrar o dispositivo ECOPDF (IP do PC, AE ECOPDF, porta 11112) e testar (Check/Verify).
   2. Storage na tecla de rotina (Store no GE, Acquire no Philips), sem multiframe, sem raw data, SR sim. Tirar o ECOPDF de qualquer outra tecla (P1) para não duplicar.
   3. Worklist apontando para o mesmo ECOPDF/11112. No GE, o fluxo com Worklist vira o Padrão.
   4. Fluxo de reenvio (Exportar no GE) apontando para o ECOPDF.
   5. Apagar destinos mortos e limpar o Spooler.
4. **Teste**
   1. Query no aparelho: paciente do dia aparece com nome, nascimento, peso e altura.
   2. Exame teste com cada sonda da sala (eco e linear; na sala 3, 6S e 4C). Conferir na tela tipo, número de imagens, marca do aparelho na barra "Página", medidas.
   3. Laudo pelo Medware → PDF24 → junta → final na pasta 3.
   4. Imprimir pela tela (papel foto por cima).
   5. Reenvio de um exame antigo pelo Exportar/Send.
   6. `enviar_status.bat` → "Enviado: OK"; cadastrar o nome do PC na aba PCs.
5. **Registrar** no Registro das máquinas: IP e tipo, nome do PC, MAC, marca do aparelho (4 caracteres), impressora/porta/fila, menus exatos, o que deu errado.

### 2.4 O que muda por sala

| | Tambauzinho sala 3 | Tambauzinho sala 6 | Tambauzinho sala 8 |
| --- | --- | --- | --- |
| Aparelho | GE Vivid S6 (mesmo modelo do Bessa) | Philips Affiniti 50G | GE Vivid T8 |
| Sondas | 6S (eco infantil → Ecocardiograma), 4C (Eco Fetal) | S4-2 (eco), L12-3 / "L12-3ERGO" (carótidas) | a confirmar no exame teste |
| Tecla de envio | Store | **Acquire** (mesma tecla para loop e foto; loops descartados no PC) | Store (menus do T8 diferem do S6) |
| Menus de rede | iguais ao Bessa (esperado) | Utilities → Setups → Rede/DICOM (não conferido) | não conferidos |
| Tipo de exame | Frequência não separa 6S de 4C: fetal depende da descrição ECO FETAL da worklist. Sem worklist, eco e fetal do mesmo dia podem cair juntos | Se o Affiniti mandar o nome da sonda, o tipo sai por `[tipos]`; se não, pela frequência | Idem |
| Laudo | Eco infantil pelo Medware; fetal de outra plataforma, impresso na PDF24 (reconhecido pelo conteúdo) | Medware | Medware |
| OCR (e′, carótidas) | Deve funcionar (mesma fonte GE), conferir | **Não funciona**: `fonte_vivid.json` é da fonte GE. e′ e velocidades só pelo SR, se vierem | Fonte do T8 pode diferir; conferir |
| Marca na folha | StationName (S6 não manda série) | DeviceSerialNumber, se vier | DeviceSerialNumber ou StationName |

### 2.5 O que ainda não se sabe (descobrir na instalação)

**Tambauzinho (as três salas)**
- Onde fica o banco do Medware: num servidor da unidade ou em cada PC. Define `MEDWARE_SERVIDORES` e a regra de firewall da porta 3050 no servidor. Hoje cada unidade tem o seu banco (Bessa confirmado).
- Lista da unidade nas três salas: o paciente só sai da lista na sala que recebeu as imagens; nas outras, até Excluir ou meia-noite. O seletor de médico reduz isso.
- Laudar numa sala um exame cujas imagens estão em outra: não implementado. Se for raro, laudar no PC da sala do aparelho.
- Impressora de cada sala e porta (USB ou Wi-Fi/WSD). Em WSD o Windows pode recusar a "EcoPDF Foto"; nesse caso o EcoPDF imprime tudo na padrão (sem papel foto separado) e a coluna do painel fica vazia.
- IP de cada PC e se o aparelho está na mesma sub-rede.

**Por aparelho**
- Se a worklist está licenciada (opção do aparelho) e se aceita peso/altura/sexo.
- Qual coluna mostra o médico na lista do aparelho.
- Se manda TransducerData (nome da sonda) e TransducerFrequency.
- Se manda SR de eco e de vascular, e com quais nomes de medida.
- Se manda DeviceSerialNumber.
- Philips: se o Acquire manda loops em MPEG (sintaxe que o EcoPDF não aceita → erro de envio no aparelho).
- Destinos antigos e tarefas presas no Spooler/fila.

### 2.6 Ordem sugerida

1. **Sala 3 (Vivid S6)**: mesmo aparelho do Bessa; valida o método e o Medware de Tambauzinho. Ponto novo: separação fetal x infantil pela worklist.
2. **Sala 8 (Vivid T8)**: GE com menus diferentes; confirma sondas e fonte.
3. **Sala 6 (Affiniti 50G)**: maior incerteza (Philips, Acquire, loops, OCR inexistente).

Cada sala instalada vira referência: o que for confirmado entra no Manual de instalação e na ficha da sala no Registro das máquinas, e este arquivo é atualizado só se o método mudar.
