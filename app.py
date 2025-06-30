import streamlit as st
from lxml import etree
from pathlib import Path
import fitz
import pikepdf
from PyPDF2 import PdfReader
import re
import json
import zipfile
import io
import base64
import tempfile
import pytesseract
from PIL import Image

st.set_page_config(page_title="ISDOC Validátor", layout="centered")
st.title("💾 ISDOC Validátor (kompletní)")

# Režim validace
st.markdown("### ⚙️ Zvol režim zpracování")
validation_mode = st.radio("Režim", ["Jedna faktura", "Batch z více faktur"])

# Výběr pravidel
st.markdown("### 🏢 Vyber společnost pro validaci")
rule_mode = st.radio("Pravidla", ["TV Nova s.r.o.", "Jiná společnost", "Vygenerovat z faktury"])

rules_path = None
rules = None

if rule_mode == "TV Nova s.r.o.":
    rules_path = Path("rules_nova.json")
elif rule_mode == "Jiná společnost":
    custom_rules_file = st.file_uploader("Nahraj vlastní pravidla (rules.json)", type=["json"], key="rules")
    if custom_rules_file:
        rules_path = custom_rules_file
    else:
        st.stop()

# Upload souboru
if validation_mode == "Jedna faktura":
    uploaded_files = [st.file_uploader("Nahraj fakturu:", type=["pdf", "xml", "isdoc"], key="single")]
else:
    uploaded_files = st.file_uploader("Nahraj více faktur (ZIP nebo víc PDF/XML)", type=["zip", "pdf", "xml", "isdoc"], accept_multiple_files=True, key="batch")

# ===== Pomocné funkce =====
def extract_with_fitz(pdf_bytes):
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            attachments = doc.attachments()
            for fname, info in attachments.items():
                if fname.lower().endswith((".xml", ".isdoc")):
                    return info["file"], f"fitz global: {fname}"
            for page in doc:
                for f in page.get_files():
                    if f["name"].lower().endswith((".xml", ".isdoc")):
                        return f["file"], f"fitz page: {f['name']}"
    except Exception as e:
        return None, f"fitz error: {e}"
    return None, None

def extract_from_text(pdf_bytes):
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            full_text = "".join(page.get_text() for page in doc)
        match = re.search(r'(<Invoice[^>]+xmlns="http://isdoc.cz/namespace/2013"[^>]*>.*?</Invoice>)', full_text, re.DOTALL)
        if match:
            return match.group(1).encode(), "fitz text"
    except Exception as e:
        return None, f"text error: {e}"
    return None, None

def extract_from_binary(pdf_bytes):
    try:
        text = pdf_bytes.decode("utf-8", errors="ignore")
        match = re.search(r'(<Invoice[^>]+xmlns="http://isdoc.cz/namespace/2013"[^>]*>.*?</Invoice>)', text, re.DOTALL)
        if match:
            return match.group(1).encode(), "binary search"
    except Exception as e:
        return None, f"binary error: {e}"
    return None, None

def extract_from_xrefs(pdf_bytes):
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i in range(1, doc.xref_length()):
                try:
                    data = doc.xref_stream(i)
                    if data:
                        match = re.search(rb'(<Invoice[^>]+xmlns="http://isdoc.cz/namespace/2013"[^>]*>.*?</Invoice>)', data, re.DOTALL)
                        if match:
                            return match.group(1), f"xref {i}"
                except:
                    continue
    except Exception as e:
        return None, f"xref error: {e}"
    return None, None

def extract_base64(pdf_bytes):
    try:
        text = pdf_bytes.decode("utf-8", errors="ignore")
        match = re.search(r"PD94bWwgdmVyc2lvbj0i[^\"]+", text)

        if match:
            decoded = base64.b64decode(match.group(0))
            if b"<Invoice" in decoded:
                return decoded, "base64 decode"
    except Exception as e:
        return None, f"base64 error: {e}"
    return None, None

def extract_ocr(pdf_bytes):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        doc = fitz.open(tmp_path)
        for page_index in range(len(doc)):
            pix = doc[page_index].get_pixmap()
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = pytesseract.image_to_string(img)
            match = re.search(r'(<Invoice[^>]+xmlns="http://isdoc.cz/namespace/2013"[^>]*>.*?</Invoice>)', text, re.DOTALL)
            if match:
                return match.group(1).encode(), "OCR"
    except Exception as e:
        return None, f"OCR error: {e}"
    return None, None

def validate_xml(xml_data: bytes, rules: dict):
    errors = []
    values = {}
    try:
        root = etree.fromstring(xml_data)
        tree = etree.ElementTree(root)
        nsmap = root.nsmap.copy()
        ns = {"ns": nsmap.get(None, "")}

        # Validace dle pravidel
        for path in rules.get("required_fields", []):
            xp = "//" + "/".join([f"ns:{p}" for p in path.split("/")])
            result = tree.xpath(xp, namespaces=ns)
            if not result:
                errors.append(f"Chybí požadované pole: `{path}`")
            elif hasattr(result[0], "text"):
                values[path] = result[0].text.strip()

        for path in rules.get("optional_fields", []):
            xp = "//" + "/".join([f"ns:{p}" for p in path.split("/")])
            result = tree.xpath(xp, namespaces=ns)
            if result and hasattr(result[0], "text"):
                values[path] = result[0].text.strip()
            else:
                values[path] = "–"

        for path, expected in rules.get("expected_values", {}).items():
            xp = "//" + "/".join([f"ns:{p}" for p in path.split("/")])
            result = tree.xpath(xp, namespaces=ns)
            found = result[0].text.strip() if result else None
            if found != expected:
                errors.append(f"Neshoda v hodnotě `{path}`: očekáváno `{expected}`, nalezeno `{found}`")
            values[path] = found or "–"

        # Výpis specifických hodnot
        st.markdown("### 📋 Výpis základních informací z faktury")

        field_map = {
            field_map = {
                    "ID faktury": "ID",
                    "Dodavatel": "AccountingCustomerParty/Party/PartyName/Name",
                    "Číslo objednávky": "OrderReference/ID",
                    "Číslo dodacího listu": "DespatchAdviceReference/ID",
                    "Číslo faktury": "ID",
                    "Variabilní symbol": "VariableSymbol",
                    "Částka bez DPH": "LegalMonetaryTotal/TaxExclusiveAmount",  # často chybí, záloha
                    "Částka k úhradě": "LegalMonetaryTotal/PayableAmount",
                    "Datum přijetí": "ReceivedDate",  # málokdy se vyskytuje
                    "Datum splatnosti": "DueDate",
                    "Datum DUZP": "TaxPointDate",
                    "Číslo bankovního účtu": "PaymentMeans/PayeeFinancialAccount/ID",
                    "Kód banky": "PaymentMeans/PayeeFinancialAccount/FinancialInstitutionBranch/ID",
                    "Popis": "Note",
                    "IČO výstavce": "AccountingSupplierParty/Party/PartyIdentification/ID",
                    "DIČ výstavce": "AccountingSupplierParty/Party/PartyTaxScheme/CompanyID",
                    "DIČ příjemce": "AccountingCustomerParty/Party/PartyTaxScheme/CompanyID",
                    "Číslo smlouvy": "ContractDocumentReference/ID",
                    "Číslo splátky": "InstallmentSequenceNumber"
                    }
                 }
        


        for label, path in field_map.items():
            xp = "//" + "/".join([f"ns:{p}" for p in path.split("/")])
            result = tree.xpath(xp, namespaces=ns)
            value = result[0].text.strip() if result and hasattr(result[0], "text") else "–"
            st.markdown(f"**{label}**: {value}")

    except Exception as e:
        errors.append(f"Chyba při zpracování XML: {e}")
    return errors, values


def generate_rules_from_xml(xml_data: bytes):
    try:
        root = etree.fromstring(xml_data)
        tree = etree.ElementTree(root)
        rules = {"required_fields": [], "optional_fields": [], "expected_values": {}}
        for element in root.xpath(".//*"):
            if element.text and element.text.strip():
                path_parts = []
                current = element
                while current is not None and current.tag != root.tag:
                    tag = etree.QName(current).localname
                    path_parts.insert(0, tag)
                    current = current.getparent()
                path_parts.insert(0, etree.QName(root).localname)
                path = "/".join(path_parts)
                rules["expected_values"][path] = element.text.strip()
        return rules
    except Exception as e:
        st.error(f"Chyba při generování pravidel: {e}")
        return {}

def process_file(data, name):
    xml_data, method = None, None
    if name.lower().endswith(".pdf"):
        with open("temp.pdf", "wb") as f:
            f.write(data)
        for extractor in [extract_with_fitz, extract_from_text, extract_from_binary, extract_from_xrefs, extract_base64, extract_ocr]:
            xml_data, method = extractor(data)
            if xml_data:
                break
    else:
        xml_data = data
        method = "přímý soubor"

    if not xml_data:
        st.error("❌ Nepodařilo se extrahovat ISDOC.")
        return

    st.success(f"✅ ISDOC extrahován metodou: {method}")

    if rule_mode == "Vygenerovat z faktury":
        rules = generate_rules_from_xml(xml_data)
        st.markdown("### 🛠 Vygenerovaná pravidla")
        st.code(json.dumps(rules, indent=2, ensure_ascii=False), language="json")
        st.download_button("📅 Stáhnout pravidla jako JSON", json.dumps(rules, indent=2), file_name="rules_generated.json")
    else:
        rules = json.loads(rules_path.read_text()) if isinstance(rules_path, Path) else json.load(rules_path)
        errors, values = validate_xml(xml_data, rules)
        if errors:
            st.error("❌ Faktura nesplňuje požadavky:")
            for e in errors:
                st.markdown(f"- {e}")
        else:
            st.success("✅ Faktura splňuje všechny požadavky.")
        st.markdown("### 📋 Výpis hodnot:")
        for k, v in values.items():
            st.markdown(f"**{k}**: {v}")

# ===== Zpracování =====
if uploaded_files:
    for file in uploaded_files:
        if file is None:
            continue
        st.markdown(f"### 📄 Zpracovávám: `{file.name}`")
        if file.name.lower().endswith(".zip"):
            with zipfile.ZipFile(file) as archive:
                for name in archive.namelist():
                    with archive.open(name) as inner_file:
                        st.markdown(f"#### 📄 `{name}`")
                        data = inner_file.read()
                        process_file(data, name)
        else:
            data = file.read()
            process_file(data, file.name)
