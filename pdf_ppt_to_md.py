
import io
import json
import base64
import hashlib
import subprocess
import fitz
import re
import html
import requests
from pathlib import Path
from PIL import Image
import logging
import os
import unicodedata
from dotenv import load_dotenv
import pytesseract
from pdf2image import convert_from_path
import torch
from transformers import CLIPProcessor, CLIPModel
from huggingface_hub import snapshot_download

from docling_core.types.doc import ImageRefMode
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

import warnings
load_dotenv()
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STRUCTURE_REGEX = re.compile(
    r"^([IVX]+|\d+|[A-Z])[\.\)]\s+",
    re.IGNORECASE
)
ARABIC_RE = re.compile(r'[\u0600-\u06FF]')
NOISE_PATTERNS = [
    r"^[£€$]+$",
    r"^[-–—_]{2,}$",
    r"^[*•·]{2,}$",
    r"^[^\w\u0600-\u06FF]+$"  # uniquement symboles
]
HEADER_KEYWORDS = [
    "ASSURANCES MAGHREBIA",
    "MAGHREBIA",
    "SOCIETE D'ASSURANCES",
    "شركة التأمين",
    "تأمينات مغربية",
    "Dépôt du",
    "إيداع بتاريخ",
    "Siège Social",
    "مقرها الاجتماعي",
    "Tél",
    "الهاتف",
    "Fax",
    "فاكس",
    "E-mail",
    "البريد الالكتروني",
    "www"
]
MAX_PAGES_WITH_IMAGES = 60
BATCH_SIZE = 15
class PDFProcessingPipeline:

    def __init__(self):

        self.llama_url = os.getenv("LLAMA_URL")
        self.model_name = os.getenv("MODEL_NAME")
        self.image_scale = float(os.getenv("IMAGE_SCALE", 2.0))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "./image_cache"))
        self.cache_dir.mkdir(exist_ok=True)
        self.detected_dir = Path(os.getenv("DETECTED_DIR", "./detected_images"))
        (self.detected_dir / "useful").mkdir(parents=True, exist_ok=True)
        (self.detected_dir / "not_useful").mkdir(parents=True, exist_ok=True)
        self.clip_repo = os.getenv("CLIP_REPO", "laion/CLIP-ViT-B-32-laion2B-s34B-b79K")
        self.mode = os.getenv("DOCLING_MODE", "dev")
        log.warning("DOCLING_MODE = %s", self.mode)

        # ------------------------------------------------------------------------------------------------------------------
        #               LOAD CLIP (GPU/CPU)
        # ------------------------------------------------------------------------------------------------------------------
        log.info(" Loading CLIP model...")
        if torch.cuda.is_available():
            self.device = "cuda"
            log.info(f" GPU detected → {torch.cuda.get_device_name(0)}")
        else:
            self.device = "cpu"
            log.warning("******** No GPU detected → Using CPU mode ********")
        clip_local = snapshot_download(repo_id=self.clip_repo, local_files_only=False)
        self.clip_model = (CLIPModel.from_pretrained(clip_local, local_files_only=True).to(self.device))
        self.clip_processor = CLIPProcessor.from_pretrained(clip_local, local_files_only=True)

        log.info("******** CLIP initialized successfully ********")
    #-------------------------------------------------------------------------------------------------------------------
    #                                               Page Count
    #-------------------------------------------------------------------------------------------------------------------

    def get_pdf_page_count(self, pdf_path: Path) -> int:
        doc = fitz.open(pdf_path)
        n = doc.page_count
        doc.close()
        return n
    #-------------------------------------------------------------------------------------------------------------------
    #                                               PDF BATCHES
    #-------------------------------------------------------------------------------------------------------------------
    def iter_pdf_batches(self, total_pages, batch_size=40):
        for start in range(1, total_pages + 1, batch_size):
            end = min(start + batch_size - 1, total_pages)
            yield start, end

    # ------------------------------------------------------------------------------------------------------------------
    #                                                  CLEAR FOLDERS
    # ------------------------------------------------------------------------------------------------------------------
    def clear_directories(self):
        """
        Vide  le contenu des dossiers cache et detected_images,
        """
        import shutil
        folders = [

            self.detected_dir / "useful",
            self.detected_dir / "not_useful"
        ]
        for folder in folders:
            for item in folder.iterdir():
                if item.is_file():
                    item.unlink()  # delete file
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                    item.mkdir(exist_ok=True)
        log.info("******** Cache and detected images directories emptied ********")

    # ------------------------------------------------------------------------------------------------------------------
    #                                           CONVERT PPT / PPTX TO PDF
    # ------------------------------------------------------------------------------------------------------------------

    def ppt_to_pptx(self, ppt_path):
        """Convertit un fichier PowerPoint ancien format (.ppt) en format moderne (.pptx) à l'aide de LibreOffice en mode headless."""
        ppt_path = Path(ppt_path)
        pptx_path = ppt_path.with_suffix(".pptx")
        # Skip si déjà converti
        if pptx_path.exists():
            log.info("******** PPTX already exists → skipping conversion")
            return pptx_path
        cmd = [
            "soffice",
            "--headless",
            "--convert-to", "pptx",
            "--outdir", str(ppt_path.parent),
            str(ppt_path)
        ]
        subprocess.run(cmd, check=True)
        if not pptx_path.exists():
            raise RuntimeError(" ******** PPT → PPTX conversion failed ********")
        return pptx_path

    def pptx_to_pdf(self, pptx_path):
        """ Convertit un fichier PowerPoint moderne (.pptx) en PDF à l'aide de LibreOffice en mode headless. """
        pptx_path = Path(pptx_path)
        pdf_path = pptx_path.with_suffix(".pdf")
        cmd = [
            "soffice",
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(pptx_path.parent),
            str(pptx_path)
        ]
        subprocess.run(cmd, check=True)
        return str(pdf_path)

    def ppt_or_pptx_to_pdf(self, input_path):
        """Convertit automatiquement un fichier PowerPoint (.ppt ou .pptx) en PDF."""
        input_path = Path(input_path)
        # .ppt → .pptx
        if input_path.suffix.lower() == ".ppt":
            log.info("******** PPT detected → converting to PPTX ********")
            input_path = self.ppt_to_pptx(input_path)
        # .pptx → .pdf
        if input_path.suffix.lower() == ".pptx":
            log.info("******** PPTX detected → converting to PDF ********")
            return self.pptx_to_pdf(input_path)
        return None

    # ------------------------------------------------------------------------------------------------------------------
    #                                           POST PROECESSING Docling
    # ------------------------------------------------------------------------------------------------------------------
    def remove_repeated_slide_footer_noise(self, md_text: str, max_span: int = 10,min_occurrences: int = 3) -> str:
        """
        Supprime un motif de bruit répété à la fin de chaque slide PPT,

        """
        lines = md_text.splitlines()
        def normalize(s: str) -> str:
            return re.sub(r"[^\w\u0600-\u06FF]", "", s.lower())
        def is_noise_token(s: str) -> bool:
            s = s.strip()
            if not s:
                return False
            if len(s) > 15:
                return False
            if not s.replace("ô", "o").isalpha():
                return False
            if ARABIC_RE.search(s):
                return False
            return True
        # 1) détecter les tokens répétés
        tokens = []
        for i, l in enumerate(lines):
            s = l.strip()
            if is_noise_token(s):
                tokens.append((i, normalize(s)))
        from collections import Counter
        counts = Counter(t for _, t in tokens)
        repeated_tokens = {t for t, c in counts.items() if c >= min_occurrences}
        if not repeated_tokens:
            return md_text
        # 2) supprimer les groupes de tokens proches
        to_remove = set()
        for i in range(len(tokens)):
            start_idx, _ = tokens[i]
            group = [tokens[i]]
            for j in range(i + 1, len(tokens)):
                idx, tok = tokens[j]
                if idx - start_idx <= max_span:
                    group.append(tokens[j])
                else:
                    break
            group_tokens = {t for _, t in group}
            # si plusieurs tokens répétés apparaissent proches → footer
            if len(group_tokens & repeated_tokens) >= 2:
                for idx, _ in group:
                    to_remove.add(idx)
        cleaned = [l for i, l in enumerate(lines) if i not in to_remove]
        return "\n".join(cleaned)

    def promote_missing_titles(self, md_text: str) -> str:
        """
        Transforme les lignes de type titre PPT en titres Markdown.
        """
        cleaned = []
        for line in md_text.splitlines():
            s = line.strip()

            # Déjà un titre markdown (## ou ### etc.)
            if re.match(r"^#{2,6}\s*", s):
                cleaned.append(line)
                continue
            # Titres numérotés (tolérant OCR / PPT)
            if (
                    re.match(r"^\d+(\.\d+)*\.?\s*[A-Za-zÀ-ÿ]", s)
                    and len(s) < 150
                    and not s.endswith(",")
            ):
                cleaned.append("## " + s)
            else:
                cleaned.append(line)

        return "\n".join(cleaned)
    def normalize_numbering(self, md_text: str) -> str:
        """Supprime une duplication simple sur une même ligne"""
        lines = []
        for line in md_text.splitlines():
            # 2. 2.X → 2.X
            line = re.sub(r"^\s*\d+\.\s+(\d+\.)", r"\1", line)
            # 5. 5Titre → 5. Titre
            line = re.sub(r"^(\d+)\.(\S)", r"\1. \2", line)
            lines.append(line)
        return "\n".join(lines)

    def remove_inline_duplication(self, md_text: str) -> str:
        cleaned = []
        for line in md_text.splitlines():
            s = re.sub(r"\s+", " ", line).strip()
            # cas: X X
            m = re.match(r"^(.+?)\s+\1$", s, flags=re.IGNORECASE)
            if m:
                cleaned.append(m.group(1))
            else:
                cleaned.append(line)
        return "\n".join(cleaned)

    def remove_repeated_title_lines(self, md_text: str) -> str:
        lines = md_text.splitlines()
        cleaned = []
        prev_norm = None
        def norm(s):
            return re.sub(r"\s+", " ", s.lower().strip())
        for line in lines:
            cur = line.strip()
            # normalisation sans ##
            cur_norm = norm(re.sub(r"^##\s+", "", cur))
            if prev_norm and cur_norm == prev_norm:
                # on saute la ligne dupliquée
                continue
            cleaned.append(line)
            prev_norm = cur_norm
        return "\n".join(cleaned)

    def remove_hybrid_inline_duplication(self, md_text: str) -> str:
        cleaned = []
        for line in md_text.splitlines():
            s = re.sub(r"\s+", " ", line).strip()
            # split en deux moitiés possibles
            parts = s.split(" ")
            mid = len(parts) // 2
            left = " ".join(parts[:mid])
            right = " ".join(parts[mid:])
            def norm(x):
                return re.sub(r"[^\w\u0600-\u06FF]", "", x.lower())
            if mid > 0 and norm(left) == norm(right):
                cleaned.append(left)
            else:
                cleaned.append(line)
        return "\n".join(cleaned)
    def remove_alpha_title_duplication(self, md_text: str) -> str:
        cleaned = []
        for line in md_text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                title = s[3:].strip()
                words = title.split()
                if len(words) == 2 and words[0].lower() == words[1].lower():
                    cleaned.append("## " + words[0])
                    continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def remove_semantic_inline_title_duplication(self, md_text: str) -> str:
        cleaned = []
        def normalize(s: str) -> str:
            s = s.lower()
            s = re.sub(r"^\d+(\.\d+)*\s*", "", s)  # supprimer numérotation
            s = re.sub(r"[^\w\u0600-\u06FF]+", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s
        for line in md_text.splitlines():
            raw = line.strip()
            # retirer ##
            prefix = ""
            content = raw
            if raw.startswith("##"):
                prefix = "## "
                content = raw[2:].strip()
            # split possible (au premier point long ou double espace)
            parts = re.split(r"\.\s+(?=\d)|\s{2,}", content)
            if len(parts) == 2:
                left, right = parts
                if normalize(left) == normalize(right):
                    cleaned.append(prefix + left.strip())
                    continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def normalize_titles(self, md_text: str) -> str:
        md_text = self.remove_repeated_slide_footer_noise(md_text)
        md_text = self.promote_missing_titles(md_text)
        md_text = self.normalize_numbering(md_text)
        md_text = self.remove_semantic_inline_title_duplication(md_text)
        md_text = self.remove_inline_duplication(md_text)
        md_text = self.remove_hybrid_inline_duplication(md_text)
        md_text = self.remove_repeated_title_lines(md_text)
        md_text = self.remove_alpha_title_duplication(md_text)
        return md_text

    def clean_markdown_spacing(self, text):
        """
        Nettoie les espaces inutiles dans le Markdown lorsqu'on supprime les images inutiles:
        """
        lines = text.split("\n")
        cleaned = []
        empty_count = 0
        for line in lines:
            if line.strip() == "":
                empty_count += 1
                if empty_count <= 1:
                    cleaned.append("")
            else:
                empty_count = 0
                cleaned.append(line.rstrip())

        return "\n".join(cleaned)

    def clean_docling_artifacts(self, text: str) -> str:
        """
        Nettoyage SAFE et général du texte Docling / PPT
        - Corrige uniquement les artefacts techniques
        """
        # 1. Normalisation Unicode fiable
        text = html.unescape(text)
        text = unicodedata.normalize("NFKC", text)
        # 2. Suppression des glyphes Docling
        text = re.sub(r"GLYPH<\d+>", "", text)
        text = re.sub(r"<\d+>", "", text)
        # 3. Apostrophes homogènes
        text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
        # 4. & parasite au milieu d’un mot
        text = re.sub(r"(\w)\s*&\s*(\w)", r"\1\2", text)
        text = re.sub(r"(?<!\()\b([a-zA-Z])\s*\)\s*", r"\1 ", text)
        # 5. Nettoyage des espaces SANS destruction
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()


    def has_broken_accents(self, source, pages_to_test: int = 2) -> bool:
        """
        Détection générique d'encodage / accents cassés.
        - source: Path (PDF)  → analyse rapide sur 1–2 pages
        - source: str (texte) → analyse directe
        """

        try:
            if isinstance(source, Path):
                doc = fitz.open(source)
                text_parts = []
                for i in range(min(pages_to_test, doc.page_count)):
                    page = doc.load_page(i)
                    text = page.get_text()
                    if text.strip():
                        text_parts.append(text)
                doc.close()
                if not text_parts:
                    return False
                text = "\n".join(text_parts)
            elif isinstance(source, str):
                if not source.strip():
                    return False
                text = source
            else:
                raise TypeError(f"Unsupported source type: {type(source)}")
            # Glyphes cassés forts (Ø, Ł, Ð, …)
            strong_bad_chars = set("�ØÐÞŁ")
            strong_count = sum(text.count(c) for c in strong_bad_chars)
            if strong_count >= 2:
                return True
            # Bruit latin Docling (Ølaboratisn, systŁme…)
            latin_noise_words = re.findall(r"\b\w*[ØŁÞÐ]\w*\b", text)
            if len(latin_noise_words) >= 2:
                return True

            return False

        except Exception:
            log.exception("Error while checking broken accents")
            return False

    def fix_misplaced_accents(self,text: str) -> str:
        rules = [
            (r"\blé\s+([a-zàâçéèêëîïôûùüÿñæœ])", r"le \1"),
            (r"\bdé\s+([a-zàâçéèêëîïôûùüÿñæœ])", r"de \1"),
            (r"\bét\s+([a-zàâçéèêëîïôûùüÿñæœ])", r"ét\1"),
            (r"\blés\s+([a-zàâçéèêëîïôûùüÿñæœ])", r"les \1"),
        ]

        for pattern, repl in rules:
            text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

        return text

    def fix_docling_latin_noise(self, text: str) -> str:
        replacements = {
            "Ø": "é",
            "Ł": "è",
            "Ð": "d",
            "Þ": "t",
        }
        for bad, good in replacements.items():
            text = text.replace(bad, good)
        return text


    # -----------------------------------------------------------------------------------------------------------------
    #                                              POST PROECESSING OCR
    # -----------------------------------------------------------------------------------------------------------------
    def is_gibberish(self, line):
        words = re.findall(r"[a-zA-Z\u0600-\u06FF]{2,}", line)
        if not words:
            return True

        short_words = sum(1 for w in words if len(w) <= 2)
        if short_words / len(words) > 0.6:
            return True

        return False
    def normalize_line(self, s: str) -> str:
        s = s.lower().strip()
        # unifier apostrophes/quotes
        s = s.replace("‘", "'").replace("’", "'").replace("`", "'")
        # enlever accents
        s = ''.join(
            c for c in unicodedata.normalize('NFKD', s)
            if not unicodedata.combining(c))
        # enlever ponctuation “non utile” + espaces
        s = re.sub(r"[^a-z0-9\u0600-\u06ff\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def is_repeated_header(self, line):
        HEADER_KEYWORDS_NORM = [self.normalize_line(k) for k in HEADER_KEYWORDS]
        l = self.normalize_line(line)
        return any(k in l for k in HEADER_KEYWORDS_NORM)

    def clean_ocr_text(self, text):
        cleaned = []
        seen_headers = set()

        for line in text.splitlines():
            l = line.strip()
            if not l:
                continue
            # bruit
            if self.is_gibberish(l):
                continue
            # entêtes répétées (une seule fois max)
            if self.is_repeated_header(l):
                key = l.lower()
                if key in seen_headers:
                    continue
                seen_headers.add(key)
                #cleaned.append(l)
                continue  # on supprime même la première occurrence
            cleaned.append(l)
        return "\n".join(cleaned)


    # -----------------------------------------------------------------------------------------------------------------
    #                                          OCR PDF WITH TESSERACT (FR/AR)
    # -----------------------------------------------------------------------------------------------------------------
    def detect_line_lang(self, line, min_ratio=0.2):
        line = line.strip()
        if not line:
            return "other"
        arabic_chars = len(ARABIC_RE.findall(line))
        total_letters = sum(c.isalpha() for c in line)
        if total_letters == 0:
            return "other"
        if arabic_chars / total_letters >= min_ratio:
            return "ar"
        return "fr"

    def split_text_ar_fr(self, text):
        fr_lines = []
        ar_lines = []

        for line in text.splitlines():
            lang = self.detect_line_lang(line)
            # filtrage bruit simple
            if len(line.strip()) < 4:
                continue
            if lang == "ar":
                ar_lines.append(line)
            elif lang == "fr":
                fr_lines.append(line)
        fr_text = "\n".join(fr_lines)
        ar_text = "\n".join(ar_lines)

        return fr_text, ar_text

    def build_bilingual_markdown(self, fr_text, ar_text):
        md = []
        if fr_text.strip():
            md.append(fr_text)
        if ar_text.strip():
            md.append("\n\n---\n\n")
            md.append(ar_text)
        return "\n".join(md)

    def extract_text_tesseract(self, pdf_path):
        """
        OCR du PDF scanné avec Tesseract
        """
        log.info("******** Using Tesseract OCR for scanned PDF ********")
        log.info("******** OCR page by page ********")
        text_parts = []

        total_pages = self.get_pdf_page_count(pdf_path)

        for i in range(1, total_pages + 1):
            images = convert_from_path(
                pdf_path,
                dpi=300,
                first_page=i,
                last_page=i
            )
            text = pytesseract.image_to_string(
                images[0],
                config="--psm 1 -l fra+ara"
            )
            text_parts.append(text)
            del images

        return text_parts

    # -----------------------------------------------------------------------------------------------------------------
    #                                         DOCLING (PDF → MARKDOWN)
    # -----------------------------------------------------------------------------------------------------------------
    def convert_pdf_to_markdown(
            self,
            pdf_path: Path,
            output_md: Path,
            page_range: tuple | None = None,
            with_images: bool = True
    ):
        """
        Convertit un PDF (ou une plage de pages) en Markdown.
        - OCR page par page si scanné
        - Docling par batch si vectoriel
        """

        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(pdf_path)

        log.info("******** Converting PDF → Markdown ********")


        # ------------------------------------------------------------------
        # PDF VECTORIEL → DOCLING (PAR BATCH)
        # ------------------------------------------------------------------
        log.info(
            "******** PDF vectorial → Docling | pages=%s | images=%s ********",
            page_range if page_range else "ALL",
            with_images
        )

        options = PdfPipelineOptions()
        options.do_ocr = "auto"
        options.images_scale = 2
        options.generate_picture_images = False
        options.generate_page_images = with_images
        options.generate_table_images = with_images
        format_option = PdfFormatOption(
            pipeline_options=options,
            page_range=page_range
        )
        converter = DocumentConverter(
            format_options={InputFormat.PDF: format_option}
        )
        result = converter.convert(str(pdf_path))
        result.document.save_as_markdown(
            output_md,
            image_mode=ImageRefMode.EMBEDDED
        )
        log.info("******** Markdown generated ********")
        return output_md

    # -----------------------------------------------------------------------------------------------------------------
    #                                                CACHE
    # -----------------------------------------------------------------------------------------------------------------
    def hash_b64(self, b64):
        return hashlib.sha256(b64.encode()).hexdigest()
    def cache_get(self, key):
        file = self.cache_dir / f"{key}.json"
        return (
            json.loads(file.read_text(encoding="utf-8"))
            if file.exists() else None
        )
    def cache_set(self, key, desc):
        file = self.cache_dir / f"{key}.json"
        file.write_text(json.dumps({"desc": desc}, ensure_ascii=False), encoding="utf-8")
    # -----------------------------------------------------------------------------------------------------------------
    #                                           IMAGE FILTER (CLIP)
    # -----------------------------------------------------------------------------------------------------------------


    def is_useful_image(self, b64: str) -> bool:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")

            w, h = img.size
            aspect = max(w, h) / max(1, min(w, h))
            # Petite image quasi carrée → logo / icône
            if w < 800 and h < 800 and aspect < 1.3:
                colors = img.getcolors(maxcolors=256)
                if colors is not None:
                    log.debug("Rejected: small+flat icon-like image (w=%d h=%d aspect=%.2f)", w, h, aspect)
                    return False


            texts = [
                """A real document image containing explicit structured data such as:
                - tables with rows and columns
                - charts with axes and values
                - diagrams or flowcharts with labeled steps
                -screenshots of portail
                This image contains concrete information that can be extracted.""",

                """A non-informative image such as:
                - decorative illustration
                - background image, nature
                - photo of people, buildings or landscapes
                - generic illustration without any data
                This image must be ignored.""",

                """A minimalist symbolic image such as:
                - a logo, icon, pictogram, emblem, seal
                - flat graphic without any real data
                This image contains NO INFORMATION and must ALWAYS be discarded."""
            ]



            inputs = self.clip_processor(
                text=texts,
                images=[img],
                return_tensors="pt",
                padding=True,
                truncation=True
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self.clip_model(**inputs)
                probs = outputs.logits_per_image.softmax(dim=1)[0]
            useful = probs[0].item()
            not_useful = probs[1].item()
            logo_icon = probs[2].item()
            log.debug("CLIP scores | useful=%.3f | not_useful=%.3f | logo_icon=%.3f", useful, not_useful, logo_icon)
            if logo_icon > 0.30:
                log.debug("Rejected: logo_icon=%.3f > 0.30", logo_icon)
                return False
            if useful < 0.55:
                log.debug("Rejected: useful=%.3f < 0.55", useful)
                return False
            if useful < not_useful + 0.20:
                log.debug("Rejected: useful=%.3f < not_useful+0.20 (=%.3f)", useful, not_useful + 0.20)
                return False
            return True
        except Exception:
            log.exception("Erreur dans is_useful_image")
            return False

    # -----------------------------------------------------------------------------------------------------------------
    #                                           IMAGE DESCRIPTION (LLM)
    # -----------------------------------------------------------------------------------------------------------------

    def generate_description(self, base64_data):
        try:
            img_raw = base64.b64decode(base64_data)
            img = Image.open(io.BytesIO(img_raw))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            prompt = """
            Analyse uniquement le texte lisible dans l’image.
            IMPORTANT — LANGUE :
            - Tu dois rédiger la réponse STRICTEMENT dans la même langue que le texte visible dans l’image.
            - N’effectue aucune traduction.
            Rédige un paragraphe fluide et cohérent qui reformule fidèlement le contenu textuel de l’image.  
            Ne décris jamais la forme du graphique, ni les couleurs, ni les icônes, ni la disposition.  
            Ne déduis rien. N’ajoute aucune information absente de l’image.  
            Conserve toutes les informations importantes : dates, titres, catégories, axes, messages.
            1. Tu dois associer chaque date uniquement avec le texte qui est aligné verticalement juste en dessous ou juste au-dessus dans la même colonne visuelle.
               ➜ N’associe jamais un texte avec une date située dans une colonne voisine.
            2. Respecte exactement l’ordre de gauche à droite des colonnes dans l’image.
               ➜ Ne réordonne pas les événements, ne permute pas les années.
            Retour attendu :
            Un paragraphe clair reprenant les événements dans le même ordre que l’image, sans aucune inversion, permutation ou reformulation logique."""

            payload = {
                "model": self.model_name,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False
            }
            r = requests.post(self.llama_url, json=payload, timeout=180)
            r.raise_for_status()
            data = r.json()
            return data.get("message", {}).get("content") or data.get("response", "")
        except Exception:
            log.exception("Erreur modèle")
            return ""

    # -----------------------------------------------------------------------------------------------------------------
    #                                                  ENRICH MARKDOWN
    # -----------------------------------------------------------------------------------------------------------------
    def enrich_markdown(self, md_path, out_path):

        content = Path(md_path).read_text(encoding="utf-8")
        pattern = r"!\[[^\]]*\]\(data:image\/(?:png|jpeg|jpg);base64,([A-Za-z0-9+/=]+)\)"
        matches = re.findall(pattern, content)
        log.info(f"******** {len(matches)} images détectées dans le Markdown ********")
        for idx, img_b64 in enumerate(matches, 1):
            log.info(f"******** Image {idx}/{len(matches)} ********")
            h = self.hash_b64(img_b64)
            img_raw = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(base64.b64decode(img_b64)))

            useful = self.is_useful_image(img_b64)

            dst_dir = self.detected_dir / ("useful" if useful else "not_useful")
            dst_path = dst_dir / f"{h}.png"

            #  Écriture DIRECTE au bon endroit
            if not dst_path.exists():
                dst_path.write_bytes(img_raw)

            log.info("******** Image classée comme %s ********", "UTILE" if useful else "NON UTILE")
            if not useful:
                content = re.sub(
                    rf"!\[[^\]]*\]\(data:image/(?:png|jpeg|jpg);base64,{re.escape(img_b64)}\)",
                    "",
                    content,
                    flags=re.IGNORECASE
                )
                continue
            cached = self.cache_get(h)
            if cached:
                log.info(f"******** Description trouvée dans le cache ********")
                desc = cached["desc"]
            else:
                log.info("******** Analyse par le modèle ********")
                desc = self.generate_description(img_b64)
                self.cache_set(h, desc)
            if not cached:
                self.cache_set(h, desc)
            replacement = f"\n\n{desc}\n\n"
            content = re.sub(rf"!\[[^\]]*\]\(data:image/(?:png|jpeg|jpg);base64,{re.escape(img_b64)}\)",replacement,content,flags=re.IGNORECASE)
        # Nettoyage des espaces
        content = self.clean_markdown_spacing(content)
        # Sauvegarde finale
        Path(out_path).write_text(content, encoding="utf-8")
        log.info(f"******** Markdown final créé ********")
        return out_path
    # -----------------------------------------------------------------------------------------------------------------
    #                                                  REMOVE DUPLICATION BATCH
    # -----------------------------------------------------------------------------------------------------------------
    def deduplicate_md_batches(self, md_parts: list[str]) -> list[str]:
        """
        Supprime les batches Docling dupliqués (identiques ou quasi-identiques).
        Basé sur hash SHA256 du contenu nettoyé.
        """
        seen_hashes = set()
        unique_parts = []

        for i, part in enumerate(md_parts):
            normalized = part.strip()
            h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

            if h in seen_hashes:
                log.warning("******** DUPLICATE DOCLING BATCH DETECTED → skipped (batch #%d) ********", i)
                continue

            seen_hashes.add(h)
            unique_parts.append(part)

        log.info(
            "******** Docling batches: %d → %d after deduplication ********",
            len(md_parts),
            len(unique_parts)
        )
        return unique_parts
# =====================================================================================================================================
#                                                    PIPELINE RUN
# =====================================================================================================================================

    def run(self, file_path, output_md):
        file_path = Path(file_path)
        output_md = Path(output_md)
        temp_pdf = None
        temp_pptx = None

        # ============================================================
        # PPT / PPTX → PDF
        # ============================================================
        if file_path.suffix.lower() in [".ppt", ".pptx"]:
            log.info("******** PPT / PPTX detected → converting to PDF ********")


            if file_path.suffix.lower() == ".ppt":
                temp_pptx = self.ppt_to_pptx(file_path)
                file_path = Path(temp_pptx)

            temp_pdf = self.pptx_to_pdf(file_path)
            file_path = Path(temp_pdf)

        # ============================================================
        #  MÉTADONNÉES PDF
        # ============================================================
        total_pages = self.get_pdf_page_count(file_path)
        log.info("******** PDF pages count = %d ********", total_pages)
        with_images = total_pages <= MAX_PAGES_WITH_IMAGES
        # ============================================================
        # DÉCISION OCR
        # ============================================================
        force_ocr = False
        if force_ocr:
            log.info("******** OCR MODE ENABLED ********")
            ocr_pages = self.extract_text_tesseract(file_path)
            md_pages = []
            for page_idx, page_text in enumerate(ocr_pages):
                cleaned = self.clean_ocr_text(page_text)
                if not cleaned.strip():
                    continue

                fr_text, ar_text = self.split_text_ar_fr(cleaned)
                md = self.build_bilingual_markdown(fr_text, ar_text)

                md_pages.append(f"\n\n<!-- PAGE {page_idx + 1} -->\n\n{md}")

            final_md = "\n".join(md_pages)

            output_md.write_text(final_md, encoding="utf-8")
            return output_md

        # ============================================================
        # DOCLING PAR BATCHS (PDF VECTORIEL)
        # ============================================================
        log.info(
            "******** DOCLING MODE | batch=%d | images=%s ********",
            BATCH_SIZE,
            with_images
        )

        md_parts = []

        for start, end in self.iter_pdf_batches(total_pages, BATCH_SIZE):
            log.info("******** Processing pages %d → %d ********", start, end)

            temp_batch_md = output_md.with_suffix(f".batch_{start}_{end}.md")

            self.convert_pdf_to_markdown(
                pdf_path=file_path,
                output_md=temp_batch_md,
                page_range=(start, end),
                with_images=with_images
            )

            batch_md = temp_batch_md.read_text(encoding="utf-8")
            batch_md = self.clean_docling_artifacts(batch_md)
            batch_md = self.normalize_titles(batch_md)
            batch_md = self.fix_misplaced_accents(batch_md)
            batch_md = self.fix_docling_latin_noise(batch_md)

            if start == 1 and self.has_broken_accents(batch_md):
                log.warning("******** Broken accents detected AFTER DOCLING → IGNORING (OCR DISABLED) ********")
            md_parts.append(batch_md)
            temp_batch_md.unlink(missing_ok=True)

        # ============================================================
        # CONCATÉNATION FINALE
        # ============================================================

        md_parts = self.deduplicate_md_batches(md_parts)
        full_md = "\n\n".join(md_parts)

        #SUPPRESSION des placeholders Docling (images picture désactivées) dans le cas ou le document dépasse la taille autorisée
        full_md = re.sub(
            r"<!--.*?generate_picture_images=True.*?-->",
            "",
            full_md,
            flags=re.DOTALL
        )
        temp_md = output_md.with_suffix(".temp.md")
        temp_md.write_text(full_md, encoding="utf-8")

        if self.cache_dir.exists():
            for f in self.cache_dir.glob("*.json"):
                f.unlink()
            log.info("******** Cache cleared BEFORE enrichment ********")

        # ============================================================
        # ENRICHMENT IMAGES (CLIP + FAST FILTER)
        # ============================================================

        self.enrich_markdown(temp_md, output_md)
        temp_md.unlink(missing_ok=True)

        # ============================================================
        # NETTOYAGE
        # ============================================================
        if temp_pdf and Path(temp_pdf).exists():
            Path(temp_pdf).unlink()
            log.info("******** PDF intermédiaire supprimé ********")

        if temp_pptx and Path(temp_pptx).exists():
            Path(temp_pptx).unlink()
            log.info("******** PPTX intermédiaire supprimé ********")

        self.clear_directories()

        log.info("******** PIPELINE FINISHED SUCCESSFULLY ********")
        return output_md

# TEST
# path_file=r"C:\Users\Ines_Ben_Amor\PycharmProjects\Pdf_Converter\8.2.7.4_NC29_Prov.pdf"
# output=r"C:\Users\Ines_Ben_Amor\PycharmProjects\Pdf_Converter\test6.md"
# instance=PDFProcessingPipeline()
# test = instance.run(path_file, output)


