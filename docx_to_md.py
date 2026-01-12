from pathlib import Path
import logging
from docling.document_converter import DocumentConverter
from docling_core.types.doc import ImageRefMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("DocxDoclingPipeline")


class DocxDoclingPipeline:
    def __init__(self):

        self.converter = DocumentConverter()
    def convert_docx(self, docx_path: Path, output_md: Path):
        log.info(f"******** DOCX detected → Using Docling Word pipeline ********")
        result = self.converter.convert(str(docx_path))
        result.document.save_as_markdown(
            output_md
            #image_mode=ImageRefMode.EMBEDDED
        )
        log.info(f"******** Markdown generated from DOCX ********")
        return output_md
