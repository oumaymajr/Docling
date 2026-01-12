import shutil
import time
import uuid
import io
import tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse

# IMPORTER TES PIPELINES
from xlsx_csv_to_md import TabularDoclingPipeline
from pdf_ppt_to_md import PDFProcessingPipeline
from docx_to_md import DocxDoclingPipeline


app = FastAPI(title="Docling APIs")

# Instances globales de pipeline
tabular = TabularDoclingPipeline()
pdf_pipe = PDFProcessingPipeline()
docx_pipe = DocxDoclingPipeline()

@app.get("/health")
def health():
    return {"status": "ok"}

# ==========================================================================
#             ENDPOINT 1 : PDF / PPT / PPTX → Markdown
# ==========================================================================
@app.post("/pdf_to_md")
async def convert_pdf_to_md(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()

    if suffix not in [".pdf", ".ppt", ".pptx"]:
        raise HTTPException(
            status_code=400,
            detail="Formats acceptés : PDF, PPT, PPTX"
        )

    # Créer temp folder
    tmp_dir = Path(tempfile.gettempdir()) / f"pdf_api_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_dir / file.filename
    input_path.parent.mkdir(parents=True, exist_ok=True)
    # Ecrire fichier uploadé
    input_path.write_bytes(await file.read())
    # Chemin du fichier Markdown final
    output_md = input_path.with_suffix(".md")
    try:
        pdf_pipe.run(input_path, output_md)
        md_text = output_md.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur PDF pipeline : {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        io.BytesIO(md_text.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{output_md.name}"'}
    )

# ==========================================================================
#             ENDPOINT 2 : XLSX / CSV → Markdown
# ==========================================================================
@app.post("/xlsx_csv_to_md")
async def convert_xlsx_csv(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()

    if suffix not in [".xlsx", ".csv"]:
        raise HTTPException(
            status_code=400,
            detail="Formats acceptés : XLSX, CSV"
        )

    tmp_dir = Path(tempfile.gettempdir()) / f"tab_api_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    input_path = tmp_dir / file.filename
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(await file.read())

    try:
        if suffix == ".xlsx":
            md_text = tabular.convert_xlsx(input_path)
        else:
            md_text = tabular.convert_csv(input_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur Tabular pipeline : {e}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        io.BytesIO(md_text.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{Path(file.filename).stem}.md"'}
    )

# ==========================================================================
#             ENDPOINT 3 : DOC / DOCX → Markdown
# ==========================================================================
@app.post("/docx_to_md")
async def convert_docx_to_md(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()

    if suffix not in [".docx", ".doc"]:
        raise HTTPException(
            status_code=400,
            detail="Formats acceptés : DOC, DOCX"
        )

    tmp_dir = Path(tempfile.gettempdir()) / f"docx_api_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    input_path = tmp_dir / file.filename
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(await file.read())

    output_md = input_path.with_suffix(".md")

    try:
        docx_pipe.convert_docx(input_path, output_md)
        md_text = output_md.read_text(encoding="utf-8")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur DOCX pipeline : {e}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        io.BytesIO(md_text.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{output_md.name}"'}
    )


# to run : uvicorn docling_apis:app --host 0.0.0.0 --port 8001 --reload