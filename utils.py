"""
Utility functions for file processing and PDF handling.
Provides functions to extract and convert PDF pages to images.
"""

import os
from typing import List, Dict, Any, Optional
import fitz
import io
from PIL import Image
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)

def process_file_smart(file_path: str) -> List[Dict[str, Any]]:
    """
    Extract text from PDF or convert pages to images for OCR processing.
    Intelligently determines if PDF pages contain digital text or are scanned images.
    
    Args:
        file_path: Path to the PDF file
        
    Returns:
        List of dicts with format:
            [
                {"type": "text", "content": "extracted text"},
                {"type": "image", "content": bytes}
            ]
            
    Raises:
        FileNotFoundError: If file doesn't exist
        Exception: If PDF processing fails
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    
    try:
        doc = fitz.open(file_path)
        final_content: List[Dict[str, Any]] = []
        
        for page_num, page in enumerate(doc):
            try:
                # Try to extract text
                text = page.get_text().strip()
                
                # If text is substantial, use it; otherwise convert to image
                if len(text) > 50:
                    final_content.append({
                        "type": "text",
                        "content": text
                    })
                    log_info(logger, f"Extracted text from page {page_num + 1}")
                else:
                    # Convert page to image for OCR
                    img_dict = _convert_page_to_image(page, page_num)
                    if img_dict:
                        final_content.append(img_dict)
                        
            except Exception as e:
                log_error(logger, f"Error processing page {page_num + 1}: {e}", exception=e)
                # Try to convert to image as fallback
                img_dict = _convert_page_to_image(page, page_num)
                if img_dict:
                    final_content.append(img_dict)
        
        doc.close()
        log_info(logger, f"Successfully processed PDF with {len(final_content)} items")
        return final_content
        
    except Exception as e:
        log_error(logger, f"Error processing PDF file: {e}", exception=e)
        raise

def _convert_page_to_image(page, page_num: int) -> Optional[Dict[str, Any]]:
    """
    Convert a PDF page to PNG image.
    
    Args:
        page: PDF page object
        page_num: Page number (for logging)
        
    Returns:
        Dict with type and image bytes or None on failure
    """
    try:
        # Render at 2x quality for better OCR
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        
        # Save to bytes buffer
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        
        log_info(logger, f"Converted page {page_num + 1} to image")
        return {
            "type": "image",
            "content": buffer.getvalue()
        }
        
    except Exception as e:
        log_error(logger, f"Error converting page {page_num + 1} to image: {e}", exception=e)
        return None

def safe_file_cleanup(file_path: str) -> bool:
    """
    Safely delete a file with error handling.
    
    Args:
        file_path: Path to file to delete
        
    Returns:
        bool: True if deleted successfully, False otherwise
    """
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            log_info(logger, f"File deleted: {file_path}")
            return True
        return False
    except Exception as e:
        log_error(logger, f"Error deleting file {file_path}: {e}", exception=e)
        return False

def ensure_directory_exists(directory_path: str) -> bool:
    """
    Ensure a directory exists, create if necessary.
    
    Args:
        directory_path: Path to directory
        
    Returns:
        bool: True if directory exists or was created
    """
    try:
        if not os.path.exists(directory_path):
            os.makedirs(directory_path)
            log_info(logger, f"Directory created: {directory_path}")
        return True
    except Exception as e:
        log_error(logger, f"Error creating directory {directory_path}: {e}", exception=e)
        return False

def format_file_size(size_bytes: int) -> str:
    """
    Format bytes to human-readable file size.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        str: Formatted size string
    """
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"
