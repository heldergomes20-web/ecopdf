"""Envia um exame falso (2 imagens, sonda M5S) para o EcoPDF deste PC."""
import configparser
import io
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import JPEGBaseline8Bit, generate_uid
from pynetdicom import AE

US = "1.2.840.10008.5.1.4.1.1.6.1"
cp = configparser.ConfigParser(interpolation=None)
cp.read(Path(__file__).with_name("config.ini"), encoding="utf-8")
porta = cp["ecopdf"].getint("PORTA", 11112)
aet = cp["ecopdf"].get("AE_TITLE", "ECOPDF").strip()

estudo, serie = generate_uid(), generate_uid()
imagens = []
for i in range(2):
    im = Image.new("RGB", (800, 600))
    d = ImageDraw.Draw(im)
    d.pieslice([100, 50, 700, 650], 235, 305, fill=(90, 90, 90))
    d.text((20, 20), f"IMAGEM DE TESTE {i + 1}", fill=(255, 255, 0))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)

    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientName, ds.PatientID = "TESTE^PACIENTE", "TESTE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = estudo, serie
    ds.SOPInstanceUID, ds.SOPClassUID = generate_uid(), US
    agora = datetime.now()
    ds.StudyDate = ds.ContentDate = agora.strftime("%Y%m%d")
    ds.StudyTime = agora.strftime("%H%M%S")
    ds.ContentTime = f"{agora:%H%M}{i:02d}"
    ds.Modality, ds.Manufacturer, ds.ManufacturerModelName = "US", "TESTE", "EcoPDF"
    ds.TransducerData = ["M5S", "", ""]
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 3, "YBR_FULL_422"
    ds.Rows, ds.Columns, ds.PlanarConfiguration = 600, 800, 0
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = encapsulate([buf.getvalue()])
    ds["PixelData"].VR = "OB"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
    imagens.append(ds)

ae = AE(ae_title="TESTE")
ae.add_requested_context(US, JPEGBaseline8Bit)
assoc = ae.associate("127.0.0.1", porta, ae_title=aet)
if not assoc.is_established:
    raise SystemExit(f"FALHOU: o EcoPDF nao respondeu na porta {porta}. Rode status.bat.")
for ds in imagens:
    st = assoc.send_c_store(ds)
    print("Imagem enviada:", "OK" if st and st.Status == 0 else f"ERRO {st}")
assoc.release()
