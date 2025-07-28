# /app/api_wrapper.py (inside Docker container markitdown)
from fastapi import FastAPI, File, UploadFile, HTTPException
import tempfile
import os
import shutil
import logging


# Import markitdown as a library
try:
    from markitdown import MarkItDown
    MARKITDOWN_LIBRARY_AVAILABLE = True
    # Can create one instance if it's thread-safe and efficient
    # md_converter_instance = MarkItDown()
    # Or create a new instance for each request for better safety
except ImportError as e:
    MARKITDOWN_LIBRARY_AVAILABLE = False
    logging.critical(f"Critical error: Failed to import MarkItDown library: {e}. "
                     f"Make sure it's correctly installed in the Docker image as specified in Dockerfile.")
    # In production, you could terminate the service if the library is unavailable
    # raise RuntimeError(f"MarkItDown library could not be imported: {e}") from e


app = FastAPI(
    title="MarkItDown Conversion Service",
    description="API for converting files to Markdown using the MarkItDown library.",
    version="1.0.0"
)


logger = logging.getLogger("markitdown-api-wrapper")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


# Check that MarkItDown loaded at application startup (optional)
if not MARKITDOWN_LIBRARY_AVAILABLE:
    logger.error("Service is starting, but MarkItDown library is unavailable. Endpoints will not work.")


async def convert_file_content_with_markitdown(file_content: bytes, original_filename: str) -> str:
    """
    Converts file content (bytes) using the markitdown library.
    MarkItDown().convert() expects a file path, so we save the content to a temporary file.
    """
    if not MARKITDOWN_LIBRARY_AVAILABLE:
        logger.error("Attempting to call conversion, but MarkItDown library is unavailable.")
        raise RuntimeError("MarkItDown library is unavailable in this environment.")

    # Use the original file extension if available, or .tmp
    # Important: Make sure MarkItDown().convert() can handle PDF,
    # if you're passing PDF. Example md.convert("test.xlsx") uses .xlsx.
    # If PDF needs a different method or format, this should be considered.
    # For this API we assume it's PDF.
    file_suffix = os.path.splitext(original_filename)[1] if original_filename else ".pdf"
    if not file_suffix: # If filename has no extension, e.g. "myfile"
        file_suffix = ".pdf" # Default for this endpoint

    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_suffix) as tmp_file:
            tmp_file.write(file_content)
            temp_file_path = tmp_file.name
        
        logger.info(f"Temporary file '{temp_file_path}' created for '{original_filename}'. Starting conversion.")
        
        # Create MarkItDown instance. If it's heavy, can be optimized.
        md_converter = MarkItDown()
        result = md_converter.convert(temp_file_path) # Pass path to temporary file

        if hasattr(result, 'text_content'):
            markdown_output = result.text_content
            logger.info(f"Conversion of file '{original_filename}' via MarkItDown library successful. "
                        f"Markdown length: {len(markdown_output)}.")
            return markdown_output
        else:
            logger.error(f"MarkItDown conversion result for '{original_filename}' doesn't have 'text_content' attribute. "
                         f"Result type: {type(result)}. Content (if small): {str(result)[:200]}")
            raise ValueError("MarkItDown conversion result format doesn't match expected.")
            
    except Exception as e:
        logger.error(f"Error during conversion of file '{original_filename}' by MarkItDown library: {e}", exc_info=True)
        # Re-raise exception so FastAPI handles it and returns correct HTTP response
        raise
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.unlink(temp_file_path)
            logger.debug(f"Temporary file '{temp_file_path}' deleted.")


@app.post("/convert-document/")
async def convert_document_endpoint(file: UploadFile = File(...)):
    """
    Endpoint for converting uploaded file (presumably PDF, XLSX, DOCX, etc.,
    depending on what MarkItDown().convert() supports) to Markdown.
    """
    original_filename = file.filename if file.filename else "unknown_file"
    logger.info(f"Received file: '{original_filename}' for conversion via MarkItDown API.")
    
    if not MARKITDOWN_LIBRARY_AVAILABLE:
        logger.error("MarkItDown library is not available for processing the request.")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable: internal MarkItDown library not loaded.")

    try:
        file_content = await file.read()
        if not file_content:
            logger.warning(f"Received empty file: '{original_filename}'.")
            raise HTTPException(status_code=400, detail="Received empty file.")

        markdown_result = await convert_file_content_with_markitdown(file_content, original_filename)
            
        return {"markdown_text": markdown_result, "source_tool": "markitdown_python_library"}
    except ValueError as e: # Our error if conversion result is unexpected
        logger.error(f"Value error during conversion of '{original_filename}': {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except HTTPException: # If HTTPException was already raised (e.g., 400)
        raise
    except Exception as e: # Other errors from MarkItDown or general
        logger.error(f"Unexpected error during conversion of '{original_filename}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error occurred while processing file: {str(e)}")
    finally:
        await file.close() # Close file in any case
        logger.debug(f"File '{original_filename}' closed after processing.")


@app.get("/health", summary="Service health check")
async def health_check():
    """Checks service availability and basic functionality of MarkItDown library."""
    status_report = {"service_status": "ok", "markitdown_library_available": MARKITDOWN_LIBRARY_AVAILABLE}
    if MARKITDOWN_LIBRARY_AVAILABLE:
        try:
            _ = MarkItDown() # Attempt initialization
            status_report["markitdown_library_initialization"] = "success"
        except Exception as e:
            status_report["markitdown_library_initialization"] = f"failed: {str(e)}"
            status_report["service_status"] = "degraded" # Service works, but main function may be impaired
            logger.warning(f"Health check: MarkItDown library initialization failed: {e}")
    else:
        status_report["service_status"] = "error" # Critical error, library not imported

    if status_report["service_status"] == "error":
         raise HTTPException(status_code=503, detail=status_report)
    return status_report