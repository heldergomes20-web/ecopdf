# Regras do atualizador automático do EcoPDF (para quando for implementado)

Ainda NÃO implementado no ecopdf.py. Este arquivo registra o desenho combinado em 2026-10-30.

## Distribuição (decidido 2026-10-30: GitHub Releases, repositório público `ecopdf`)
- Kit do site/cPanel (obsoleto-site/) NÃO é mais usado.
- Cada versão = release com tag = versão (AAAA.MM.DD.N); anexos obrigatórios `EcoPDF.zip` e `EcoPDF.zip.sig` (base64 Ed25519 dos bytes do zip).
- Canal geral = release Latest (não pre-release). Canal teste = maior versão publicada (pre-release ou não). Rascunhos são ignorados.
- Claude cria o rascunho com o zip; Helder assina offline (assinar.bat) e anexa o .sig, publica como pre-release; promove a Latest após validar.
- Cliente usa a API pública `https://api.github.com/repos/<repo>/releases` (sem token; limite 60 req/h por IP é suficiente para poucas checagens/dia). Ignora release sem os dois anexos.
- Licença: todos os direitos reservados. Repo não contém dados de pacientes, IPs ou senhas.
- Esquema visual: https://claude.ai/artifact/TaMLEsB5SNaijEQUHmd3Bj

## Versões
- Nome AAAA.MM.DD.N (ex.: 2026.11.05.1); comparação numérica por partes, então 2026.10.30 < 2026.10.30.1 < 2026.10.30.2.
- Registro: /mnt/project-files/eco-pdf/HISTORICO_VERSOES.md (versão, data, canal, mudanças, salas, problemas, revertida).

## Voltar atrás (3 níveis)
1. Local automático: antes de instalar, copia a instalação atual para C:\EcoPDF\_anterior (só código e config, não dados). Se a nova não iniciar ou falhar no teste de saúde, restaura sozinho e registra no log. voltar.bat faz o mesmo à mão.
2. Todas as salas: reverter.bat no assinador publica o código antigo com número NOVO (ex.: 2026.11.05.2 = código de 2026.10.30). Nunca publicar número menor.
3. Uma sala à mão: anteriores/<v>/EcoPDF.zip + instalar.bat (o atualizador depois sobe de novo para a versão do canal).

## Cliente (ecopdf.py)
1. Canal vem do config.ini da máquina: `CANAL_ATUALIZACAO = geral` (padrão) ou `teste`. Qualquer PC vira o de teste trocando essa linha.
2. Só atualiza se versão remota for MAIOR que a local (comparação numérica por partes). Igual ou menor: não faz nada. Nunca rebaixa.
   - Assim a máquina de teste, rodando uma versão mais nova que a do geral, nunca "volta" para a do geral; quando o geral alcançar, ela segue normalmente.
   - Voltar a máquina de teste para o geral = trocar o canal; ela fica na versão que tem até o geral publicar uma maior.
   - Reverter um problema = publicar o código antigo com número NOVO (reverter.bat), nunca um número menor.
3. Consulta a API de releases; se a versão do canal for maior, baixa zip + sig dos anexos e confere a assinatura com a chave pública embutida no código. Qualquer falha: não instala, registra no log e tenta na próxima checagem.
4. Confere que o VERSAO.txt DENTRO do zip é igual à tag do release (impede reaproveitar zip antigo assinado com número novo).
5. Checa ao iniciar e a cada 2 horas; baixa e confere a assinatura na hora, mas NUNCA instala durante o dia (Helder 2026-10-30: 20 min ocioso não é seguro; PCs desligados à noite). Instala só no INÍCIO do PC, antes de abrir a porta DICOM (~1 min). A confirmar: Vivid reenvia (fila) se a porta estiver fechada nesse minuto; se não, abrir a porta, guardar o que chegar e instalar depois. atualizar_agora.bat para forçar.
6. Instalação: backup da pasta atual (C:\EcoPDF\_anterior), extrai preservando config.ini local e dados (processados, dicom, log), roda os passos do instalar.bat necessários e reinicia a tarefa. Se o EcoPDF novo não subir, restaura o backup sozinho.
7. Sem internet ou site fora do ar: segue funcionando normalmente; atualização é opcional, nunca bloqueia o trabalho.
8. Registra no log e no status.bat: canal, versão local, última checagem, resultado.

## Publicação
- Ver manual de atualização (Claude Doc https://claude.ai/artifact/4d4bQwgKBCVyYVQCS6sBbo, cópia eco-pdf/MANUAL_ATUALIZACAO.md).
