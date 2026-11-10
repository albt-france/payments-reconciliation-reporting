"""
Remove personal traces that Excel writes into a saved workbook:
  - docProps/core.xml : author / last modified by  -> AUTHOR
  - xl/workbook.xml   : the absolute folder path of the file (x15ac:absPath)
  - pivot caches      : refreshedBy (the Excel user name)

    python src/sanitize_xlsx.py excel/Monthly_Close_Pack.xlsx
"""
import re
import shutil
import sys
import zipfile

AUTHOR = "albt"


def sanitize(path: str) -> None:
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/core.xml":
                s = data.decode("utf-8")
                s = re.sub(r"<dc:creator>.*?</dc:creator>", f"<dc:creator>{AUTHOR}</dc:creator>", s)
                s = re.sub(r"<cp:lastModifiedBy>.*?</cp:lastModifiedBy>", f"<cp:lastModifiedBy>{AUTHOR}</cp:lastModifiedBy>", s)
                data = s.encode("utf-8")
            elif item.filename == "xl/workbook.xml":
                s = data.decode("utf-8")
                s = re.sub(r"<mc:AlternateContent[^>]*><mc:Choice Requires=\"x15\"><x15ac:absPath[^>]*/></mc:Choice></mc:AlternateContent>", "", s)
                data = s.encode("utf-8")
            elif item.filename.startswith("xl/pivotCache/pivotCacheDefinition"):
                s = data.decode("utf-8")
                s = re.sub(r'refreshedBy="[^"]*"', f'refreshedBy="{AUTHOR}"', s)
                data = s.encode("utf-8")
            zout.writestr(item, data)
    shutil.move(tmp, path)


if __name__ == "__main__":
    sanitize(sys.argv[1])
    print("sanitized", sys.argv[1])
