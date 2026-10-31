/**
 * EcoPDF - Painel das salas (Google Apps Script, colado na planilha da administracao).
 *
 * Cada EcoPDF manda o resumo do PC as 9h e as 15h (doPost). Uma linha por PC, sempre
 * sobrescrita. A sala vem da aba "PCs" (nome do computador -> sala), preenchida uma vez.
 * Alerta por e-mail as ~9h45 e ~15h45, so quando alguma sala nao esta OK.
 *
 * Instalar: Extensoes > Apps Script > colar este arquivo > Salvar > recarregar a planilha
 *           > menu EcoPDF > Configurar (uma vez) > Implantar como app da Web.
 * Nunca chega nome de paciente: o EcoPDF so manda numero, horario, versao e rede.
 */

var TZ = 'America/Fortaleza';            // Joao Pessoa (UTC-3, sem horario de verao)
var HORARIOS = [9, 15];                  // horas do envio (iguais a PAINEL_HORARIOS)
var TOLERANCIA_MIN = 30;                 // envio pode atrasar ate isso (PC ligando)
var DIAS_UTEIS = {1: [9, 15], 2: [9, 15], 3: [9, 15], 4: [9, 15], 5: [9, 15], 6: [9]}; // 0 = domingo: nada
var RELOGIO_MAX_MIN = 10;                // diferenca maxima PC x Google
var REPO = 'heldergomes20-web/ecopdf';   // versao publicada (Latest) no GitHub
var MAX_LINHAS = 60;
var MAX_EXTRAS = 30;               // colunas novas que o EcoPDF pode criar sozinho

var ABA = 'Salas', ABA_PCS = 'PCs', LIN0 = 4; // dados a partir da linha 4
var COLS = ['Sala', 'Situação', 'O que olhar', 'Último envio', 'PC', 'Versão',
  'IP do PC', 'AE Title : porta', 'Última conexão de aparelho', 'Última imagem', 'Aparelho (série)',
  'Última junção', 'Imagens sem laudo', 'Laudos sem imagens', 'Motivo do laudo sem imagens',
  '9 - Revisar', 'Retificados (3 dias)', 'Liberados sem imagens (7 dias)', 'Descartados (7 dias)',
  'Sumiram da pasta 2 (7 dias)', 'Erros (24 h)', 'Disco livre (GB)', 'Disco enche em (dias)', 'Imagens guardadas (GB)', 'MB por exame',
  'Memória EcoPDF (MB)', 'CPU média (%)', 'Folha pronta em (s, média / máx 24 h)', 'Impressora', 'Ligado desde',
  'IP anterior (apague depois de conferir o aparelho)', 'nivel_pc', 'problemas_pc', 'relogio_min'];
function col(nome) { return COLS.indexOf(nome) + 1; }

// ---------------------------------------------------------------- recepcao

function doPost(e) {
  var d;
  try { d = JSON.parse(e.postData.contents); } catch (err) { return resp(false, 'json'); }
  var chave = PropertiesService.getScriptProperties().getProperty('CHAVE') || '';
  if (!chave || d.chave !== chave) return resp(false, 'chave');
  var pc = limpo(d.pc, 40);
  if (!pc) return resp(false, 'pc');

  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var sh = SpreadsheetApp.getActive().getSheetByName(ABA);
    var n = Math.max(0, sh.getLastRow() - LIN0 + 1);
    var pcs = n ? sh.getRange(LIN0, col('PC'), n, 1).getValues().map(function (r) { return r[0]; }) : [];
    var i = pcs.indexOf(pc);
    if (i < 0 && n >= MAX_LINHAS) return resp(false, 'cheio');
    var linha = i < 0 ? LIN0 + n : LIN0 + i;
    var agora = new Date();
    var antes = i < 0 ? [] : sh.getRange(linha, 1, 1, COLS.length).getValues()[0];

    var ip = limpo(d.ip, 80);
    var ipAnterior = i < 0 ? '' : antes[col('IP anterior (apague depois de conferir o aparelho)') - 1];
    var ipAntes = i < 0 ? '' : antes[col('IP do PC') - 1];
    if (ipAntes && ip && ipPrincipal(ipAntes) !== ipPrincipal(ip)) ipAnterior = ipAntes;

    var c = d.ultima_conexao || {};
    var conexao = c.ip ? [limpo(c.ip, 40), limpo(c.ae, 20), limpo(c.tipo, 40), dataBr(c.em)].join(' · ') : 'nenhuma registrada';
    var relogio = '';
    var tpc = new Date(d.agora);
    if (!isNaN(tpc)) relogio = Math.round((tpc - agora) / 60000);
    var img = num(d.ultima_imagem_min);
    var ultimaImg = img === '' ? 'nenhuma' : fmt(new Date(agora.getTime() - img * 60000));

    var v = {};
    v['Sala'] = '=IFERROR(VLOOKUP(E' + linha + ',' + ABA_PCS + '!A:B,2,FALSE),"sem sala")';
    v['Último envio'] = agora;
    v['PC'] = pc;
    v['Versão'] = limpo(d.versao, 20);
    v['IP do PC'] = ip;
    v['AE Title : porta'] = limpo(d.ae_title, 16) + ' : ' + num(d.porta) + (d.ecopdf_rodando === false ? ' (FECHADA)' : '');
    v['Última conexão de aparelho'] = conexao;
    v['Última imagem'] = ultimaImg;
    v['Aparelho (série)'] = limpo(d.aparelho, 8);
    v['Última junção'] = dataBr(d.ultima_juncao) || 'nenhuma';
    v['Imagens sem laudo'] = pend(d.imagens_sem_laudo, d.imagens_sem_laudo_mais_antiga_min);
    v['Laudos sem imagens'] = pend(d.laudos_sem_imagens, d.laudos_sem_imagens_mais_antigo_min);
    v['Motivo do laudo sem imagens'] = motivos(d.motivos_laudo_sem_par);
    v['9 - Revisar'] = num(d.revisar);
    v['Liberados sem imagens (7 dias)'] = num(d.liberados_sem_imagens_7d);
    v['Descartados (7 dias)'] = num(d.descartados_7d);
    v['Sumiram da pasta 2 (7 dias)'] = num(d.fora_pasta2_7d);
    v['Retificados (3 dias)'] = num(d.retificados_3d);
    v['Erros (24 h)'] = num(d.erros_24h);
    v['Disco livre (GB)'] = num(d.disco_livre_gb);
    v['Disco enche em (dias)'] = d.disco_dias === null || d.disco_dias === undefined ? '— (não está enchendo)' : num(d.disco_dias);
    v['Imagens guardadas (GB)'] = num(d.imagens_gb);
    v['MB por exame'] = num(d.mb_por_exame);
    v['Memória EcoPDF (MB)'] = d.envio === 'manual' ? 'envio manual' : num(d.memoria_mb);
    v['CPU média (%)'] = d.envio === 'manual' ? 'envio manual' : num(d.cpu_medio_pct);
    v['Folha pronta em (s, média / máx 24 h)'] = d.folha_s_media === null || d.folha_s_media === undefined ? 'sem exame' :
      num(d.folha_s_media) + ' / ' + num(d.folha_s_max);
    v['Impressora'] = limpo(d.impressora, 120) || '—';
    v['Ligado desde'] = d.envio === 'manual' ? 'envio manual' : dataBr(d.ligado_desde);
    v['IP anterior (apague depois de conferir o aparelho)'] = ipAnterior;
    v['nivel_pc'] = ['verde', 'amarelo', 'vermelho'].indexOf(d.nivel) >= 0 ? d.nivel : 'amarelo';
    v['problemas_pc'] = (Array.isArray(d.problemas) ? d.problemas : []).map(function (p) { return limpo(p, 160); }).slice(0, 12).join('; ');
    v['relogio_min'] = relogio;
    v['Situação'] = antes[col('Situação') - 1] || '';
    v['O que olhar'] = '';
    sh.getRange(linha, 1, 1, COLS.length).setValues([COLS.map(function (k) { return v[k] === undefined ? '' : v[k]; })]);
    extras_(sh, linha, d.extras);
    conferir_();
  } finally {
    lock.releaseLock();
  }
  return resp(true);
}

/** Campos novos do EcoPDF (d.extras = {rotulo: valor}) viram colunas sozinhos, depois das
 *  fixas: versao nova do EcoPDF acrescenta informacao sem precisar trocar este script. */
function extras_(sh, linha, extras) {
  if (!extras || typeof extras !== 'object') return;
  var largura = Math.max(sh.getLastColumn(), COLS.length);
  var cab = sh.getRange(LIN0 - 1, 1, 1, largura).getValues()[0].map(String);
  Object.keys(extras).slice(0, MAX_EXTRAS).forEach(function (k) {
    var rot = limpo(k, 40);
    if (!rot || rot.charAt(0) === "'") return; // rotulo que pareceria formula: ignora
    var c = cab.indexOf(rot);
    if (c < 0) {
      if (cab.length >= COLS.length + MAX_EXTRAS) return;
      cab.push(rot); c = cab.length - 1;
      sh.getRange(LIN0 - 1, c + 1).setValue(rot).setFontWeight('bold').setBackground('#eceff1').setWrap(true);
    }
    var x = extras[k];
    sh.getRange(linha, c + 1).setValue(typeof x === 'number' ? x : limpo(x, 120));
  });
}

function resp(ok, motivo) {
  return ContentService.createTextOutput(JSON.stringify({ok: ok, motivo: motivo || ''}))
    .setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------- avaliacao

/** Recalcula Situacao / O que olhar de todas as linhas (de hora em hora e a cada envio). */
function conferir() {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try { conferir_(); } finally { lock.releaseLock(); }
}

function conferir_() {
  var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(ABA);
  var n = sh.getLastRow() - LIN0 + 1;
  var publicada = versaoPublicada();
  var esperado = ultimoHorarioEsperado(new Date());
  sh.getRange('A2').setValue('Versão publicada (Latest): ' + (publicada || '?') +
    '   ·   conferido em ' + fmt(new Date()) + '   ·   envios às 9h e 15h');
  if (n <= 0) return;
  var dados = sh.getRange(LIN0, 1, n, COLS.length).getValues();
  var saida = dados.map(function (r) {
    var prob = [], aten = [];
    var envio = r[col('Último envio') - 1];
    if (esperado && envio instanceof Date && envio < esperado) {
      return ['SEM SINAL', 'nao mandou o envio das ' + Utilities.formatDate(esperado, TZ, 'HH:mm') +
        ' (PC desligado, sem internet ou EcoPDF parado). Ultimo: ' + fmt(envio)];
    }
    var nivel = r[col('nivel_pc') - 1];
    var pp = String(r[col('problemas_pc') - 1] || '');
    (nivel === 'vermelho' ? prob : aten).push(pp);
    if (r[col('IP anterior (apague depois de conferir o aparelho)') - 1]) {
      prob.push('IP do PC mudou (antes ' + r[col('IP anterior (apague depois de conferir o aparelho)') - 1] +
        '): conferir o IP cadastrado no aparelho');
    }
    var rel = r[col('relogio_min') - 1];
    if (rel !== '' && Math.abs(rel) > RELOGIO_MAX_MIN) prob.push('relogio do PC errado (' + rel + ' min)');
    var v = String(r[col('Versão') - 1] || '');
    if (publicada && compVersao(v, publicada) < 0) aten.push('versao antiga (' + v + ')');
    prob = prob.filter(String); aten = aten.filter(String);
    var sit = prob.length ? 'PROBLEMA' : aten.length ? 'ATENÇÃO' : 'OK';
    return [sit, prob.concat(aten).join('; ')];
  });
  sh.getRange(LIN0, col('Situação'), n, 2).setValues(saida);
}

/** Horario de envio mais recente que ja deveria ter chegado (com tolerancia). */
function ultimoHorarioEsperado(agora) {
  for (var d = 0; d < 8; d++) {
    var dia = new Date(agora.getTime() - d * 86400000);
    var ymd = Utilities.formatDate(dia, TZ, 'yyyy-MM-dd');
    var semana = Number(Utilities.formatDate(dia, TZ, 'u')) % 7; // 7 = domingo -> 0
    var horas = (DIAS_UTEIS[semana] || []).slice().reverse();
    for (var k = 0; k < horas.length; k++) {
      var h = ('0' + horas[k]).slice(-2);
      var t = new Date(ymd + 'T' + h + ':00:00-03:00');
      if (t.getTime() + TOLERANCIA_MIN * 60000 <= agora.getTime()) return t;
    }
  }
  return null;
}

function versaoPublicada() {
  var p = PropertiesService.getScriptProperties();
  var em = Number(p.getProperty('VERSAO_EM') || 0);
  if (Date.now() - em < 3600000) return p.getProperty('VERSAO') || '';
  try {
    var r = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/releases/latest',
      {muteHttpExceptions: true, headers: {Accept: 'application/vnd.github+json'}});
    if (r.getResponseCode() === 200) {
      var tag = String(JSON.parse(r.getContentText()).tag_name || '').replace(/^v/, '');
      p.setProperties({VERSAO: tag, VERSAO_EM: String(Date.now())});
      return tag;
    }
  } catch (err) {}
  return p.getProperty('VERSAO') || '';
}

function compVersao(a, b) {
  var x = a.split('.').map(Number), y = b.split('.').map(Number);
  for (var i = 0; i < Math.max(x.length, y.length); i++) {
    var d = (x[i] || 0) - (y[i] || 0);
    if (d) return d;
  }
  return 0;
}

// ---------------------------------------------------------------- alerta por e-mail

/** ~9h45 e ~15h45: e-mail so se alguma sala nao estiver OK. */
function alertar() {
  if (Number(Utilities.formatDate(new Date(), TZ, 'u')) === 7) return; // domingo
  conferir();
  var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(ABA);
  var n = sh.getLastRow() - LIN0 + 1;
  if (n <= 0) return;
  var r = sh.getRange(LIN0, 1, n, col('PC')).getDisplayValues().filter(function (x) { return x[1] && x[1] !== 'OK'; });
  if (!r.length) return;
  var ordem = {'SEM SINAL': 0, 'PROBLEMA': 1, 'ATENÇÃO': 2};
  r.sort(function (a, b) { return ordem[a[1]] - ordem[b[1]]; });
  var texto = r.map(function (x) { return '• ' + x[0] + ' (' + x[col('PC') - 1] + '): ' + x[1] + ' — ' + x[2]; }).join('\n');
  var graves = r.filter(function (x) { return x[1] !== 'ATENÇÃO'; }).length;
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(),
    'EcoPDF: ' + r.length + ' sala(s) para olhar' + (graves ? ' (' + graves + ' com problema)' : ''),
    texto + '\n\nPainel: ' + ss.getUrl());
}

// ---------------------------------------------------------------- instalacao

function onOpen() {
  SpreadsheetApp.getUi().createMenu('EcoPDF')
    .addItem('Configurar (uma vez)', 'configurar')
    .addItem('Conferir agora', 'conferir')
    .addItem('Testar e-mail de alerta', 'alertar')
    .addToUi();
}

function configurar() {
  var ss = SpreadsheetApp.getActive();
  ss.setSpreadsheetTimeZone(TZ);
  var sh = ss.getSheetByName(ABA) || ss.insertSheet(ABA, 0);
  sh.getRange('A1').setValue('EcoPDF · Painel das salas').setFontSize(14).setFontWeight('bold');
  sh.getRange(LIN0 - 1, 1, 1, COLS.length).setValues([COLS]).setFontWeight('bold')
    .setBackground('#eceff1').setWrap(true);
  sh.setFrozenRows(LIN0 - 1);
  sh.setFrozenColumns(2);
  sh.setColumnWidth(1, 120); sh.setColumnWidth(2, 95); sh.setColumnWidth(3, 320);
  sh.getRange(LIN0, col('Último envio'), MAX_LINHAS, 1).setNumberFormat('dd/MM HH:mm');
  sh.getRange(LIN0, col('O que olhar'), MAX_LINHAS, 1).setWrap(true);
  sh.hideColumns(col('nivel_pc'), 3);
  var faixa = sh.getRange(LIN0, col('Situação'), MAX_LINHAS, 1);
  var regra = function (txt, cor) {
    return SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(txt).setBackground(cor)
      .setBold(true).setRanges([faixa]).build();
  };
  sh.setConditionalFormatRules([regra('OK', '#c8e6c9'), regra('ATENÇÃO', '#fff59d'),
    regra('PROBLEMA', '#ef9a9a'), regra('SEM SINAL', '#ef9a9a')]);

  var pcs = ss.getSheetByName(ABA_PCS) || ss.insertSheet(ABA_PCS);
  if (!pcs.getRange('A1').getValue()) {
    pcs.getRange('A1:C1').setValues([['Nome do computador', 'Sala', 'Observação']]).setFontWeight('bold');
    pcs.setFrozenRows(1);
  }
  var p = PropertiesService.getScriptProperties();
  if (!p.getProperty('CHAVE')) p.setProperty('CHAVE', Utilities.getUuid().replace(/-/g, '').slice(0, 20));

  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('conferir').timeBased().everyHours(1).create();
  HORARIOS.forEach(function (h) {
    ScriptApp.newTrigger('alertar').timeBased().atHour(h).nearMinute(50).everyDays(1).inTimezone(TZ).create();
  });
  conferir();
  SpreadsheetApp.getUi().alert('Painel configurado.\n\nPAINEL_CHAVE = ' + p.getProperty('CHAVE') +
    '\n\nAgora: Implantar > Nova implantação > App da Web (Executar como: eu; Quem pode acessar: Qualquer pessoa).');
}

// ---------------------------------------------------------------- utilitarios

function limpo(s, max) {
  s = String(s === undefined || s === null ? '' : s).replace(/[\r\n\t]/g, ' ').trim().slice(0, max || 80);
  return /^[=+\-@]/.test(s) ? "'" + s : s; // nunca vira formula
}
function num(x) { var n = Number(x); return x === null || x === undefined || x === '' || isNaN(n) ? '' : n; }
function pend(n, min) {
  n = num(n) || 0;
  if (!n) return 0;
  min = num(min) || 0;
  return n + ' (mais antigo: ' + (min >= 1440 ? Math.floor(min / 1440) + ' d' : min >= 60 ? Math.floor(min / 60) + ' h' : min + ' min') + ')';
}
function motivos(m) {
  if (!m || typeof m !== 'object') return '';
  return Object.keys(m).slice(0, 6).map(function (k) { return limpo(k, 30) + ' (' + (num(m[k]) || 0) + ')'; }).join(', ');
}
function fmt(d) { return Utilities.formatDate(d, TZ, 'dd/MM HH:mm'); }
function dataBr(iso) {
  if (!iso) return '';
  var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? m[3] + '/' + m[2] + ' ' + m[4] + ':' + m[5] : '';
}
function ipPrincipal(s) { return String(s).split(',')[0].trim(); }
