# Gera "DADOS DESTE PC.txt" na pasta Laudos - Nao Apagar: IP, AE e porta para o
# aparelho, e IP + endereco fisico (MAC) para a reserva no roteador.
param([string]$Porta = "11112", [string]$AE = "ECOPDF", [switch]$Abrir)

$pasta = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Laudos - Não Apagar'
New-Item -ItemType Directory -Force -Path $pasta | Out-Null
$arq = Join-Path $pasta 'DADOS DESTE PC.txt'

$ouvindo = Get-NetTCPConnection -LocalPort ([int]$Porta) -State Listen -ErrorAction SilentlyContinue
$situacao = if ($ouvindo) { 'FUNCIONANDO' } else { 'PARADO (nao esta recebendo imagens)' }

# Placas ligadas com gateway = as que realmente estao na rede da clinica
$redes = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue |
           Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' })

$L = New-Object System.Collections.Generic.List[string]
$vf = Join-Path $PSScriptRoot 'VERSAO.txt'
$versao = if (Test-Path $vf) { (Get-Content $vf -TotalCount 1).Trim() } else { '?' }
$L.Add('EcoPDF - dados deste PC (versao ' + $versao + ')')
$L.Add('Gerado em ' + (Get-Date -Format 'dd/MM/yyyy HH:mm') + ' pelo instalar.bat')
$L.Add('')
$L.Add('Computador: ' + $env:COMPUTERNAME + '    Usuario do Windows: ' + $env:USERNAME)
$L.Add('Situacao do EcoPDF: ' + $situacao)
$L.Add('')

if ($redes.Count -eq 0) {
    $L.Add('ATENCAO: nao encontrei nenhuma rede ligada neste PC.')
    $L.Add('Confira o cabo de rede e rode o instalar.bat de novo.')
}

$n = 0
foreach ($r in $redes) {
    $n++
    $ip = ($r.IPv4Address | Select-Object -First 1)
    $ad = $r.NetAdapter
    $dhcp = (Get-NetIPInterface -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).Dhcp
    $wifi = ($ad.PhysicalMediaType -match '802\.11') -or ($ad.InterfaceDescription -match 'Wi-?Fi|Wireless')
    $inicio = if ($ip.PrefixLength -eq 24) { ($ip.IPAddress.Split('.')[0..2] -join '.') + '.' } else { $null }

    if ($redes.Count -gt 1) { $L.Add("----- Rede $n de $($redes.Count): $($r.InterfaceAlias) -----") }
    $L.Add('1) CADASTRAR NO APARELHO (destino DICOM Storage)')
    $L.Add('   IP do PC:  ' + $ip.IPAddress)
    $L.Add('   AE Title:  ' + $AE)
    $L.Add('   Porta:     ' + $Porta)
    $L.Add('')
    $L.Add('2) CONFERIR A REDE')
    if ($inicio) {
        $L.Add('   O IP do aparelho tem que comecar com ' + $inicio + ' (igual a este PC).')
    } else {
        $L.Add('   Mascara de rede /' + $ip.PrefixLength + ': confira com quem cuida da rede se o aparelho esta na mesma faixa.')
    }
    $L.Add('')
    $L.Add('3) RESERVA DE IP NO ROTEADOR (passar para quem cuida da rede)')
    $L.Add('   IP a reservar:              ' + $ip.IPAddress)
    $L.Add('   Endereco fisico (MAC):      ' + $ad.MacAddress)
    $L.Add('   Placa de rede:              ' + $r.InterfaceAlias + ' (' + $ad.InterfaceDescription + ')')
    $L.Add('   Roteador (gateway):         ' + $r.IPv4DefaultGateway.NextHop)
    if ($dhcp -eq 'Disabled') {
        $L.Add('   IP ja esta fixo no Windows: a reserva nao e necessaria.')
    } else {
        $L.Add('   IP automatico (DHCP): sim -> PEDIR A RESERVA, senao o IP pode mudar e o aparelho para de enviar.')
    }
    if ($wifi) {
        $L.Add('')
        $L.Add('   ATENCAO: este PC esta no Wi-Fi. Prefira cabo: o Wi-Fi muda de IP e cai com mais facilidade.')
    }
    $L.Add('')
}

if ($redes.Count -gt 1) {
    $L.Add('Este PC tem mais de uma rede ligada. Use a que tem o IP comecando igual ao do aparelho.')
    $L.Add('')
}
$L.Add('Depois de cadastrar: fazer o Check/Verify no aparelho e anotar o IP na tabela de salas do manual.')

$L | Out-File -FilePath $arq -Encoding UTF8
Write-Host ('  Dados salvos em: ' + $arq)
if ($Abrir) { Start-Process notepad.exe -ArgumentList ('"' + $arq + '"') }
