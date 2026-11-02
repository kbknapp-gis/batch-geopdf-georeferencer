"""
batch_geopdf_georeferencer.py
=============================

Batch-georeferences folders of non-spatial PDF maps into Geospatial PDFs
projected to NAD83 / UTM (Zone 12N or 13N, chosen from each map's center
longitude).

How it works
------------
1. Scans every PDF in a folder. PDFs that already carry geospatial tags
   (/VP, /LGIDict, /Measure) are skipped unless FORCE_OVERWRITE is True.
2. Looks up each remaining PDF's WGS84 bounding box in a CSV index
   (columns: filename, min_lon, min_lat, max_lon, max_lat).
3. Builds four corner Ground Control Points, warps the page in GDAL virtual
   memory (/vsimem/), and writes a compressed ISO 32000 Geospatial PDF to
   FIXED_PDFS/ with a "_georef.pdf" suffix.
4. PDFs with no CSV record are moved to UNREFERENCED_PDFS/ for review.

Requirements: Python 3, GDAL (osgeo), pypdf.

Usage: edit the CONFIGURATION block below, then run
    python batch_geopdf_georeferencer.py
"""

import os
import shutil
import csv
from pypdf import PdfReader
from osgeo import gdal, osr

# =====================================================================
# CONFIGURATION: edit these for your environment
# =====================================================================
# Folder containing the PDF maps to process (placeholder path).
MAPS_FOLDER = r"C:\path\to\your\pdf_maps"

# CSV index file name, expected inside MAPS_FOLDER.
CSV_INDEX_NAME = "map_index.csv"
# =====================================================================

# Force GDAL exceptions to catch format issues cleanly
gdal.UseExceptions()

# --- FORCE RESET SWITCH ---
# Set to True to completely strip out old, inaccurate geospatial metadata
# and replace it with tight tolerances and the new aspect-ratio warp engine.
FORCE_OVERWRITE = False

def check_pdf_spatial(file_path):
    """
    Scans a PDF's internal dictionary strings for existing geospatial tags.
    """
    try:
        reader = PdfReader(file_path)
        for page in reader.pages:
            page_data = str(page)
            if '/VP' in page_data or '/LGIDict' in page_data or '/Measure' in page_data:
                return True
    except Exception as e:
        print(f"Error reading structure of {os.path.basename(file_path)}: {e}")
    return False

def georeference_file_to_utm(pdf_path, output_path, bbox_wgs84):
    """
    Step 1: Translates WGS84 bounds to an in-memory Virtual GeoTIFF (/vsimem/) with GCPs.
    Step 2: Warps the virtual raster using a strict Similarity Transformation (dstSRS).
    Step 3: Translates the final warp grid back into a clean, optimized Geospatial PDF.
    """
    vsimem_temp = None
    vsimem_warp = None
    try:
        min_lon, min_lat, max_lon, max_lat = bbox_wgs84
        center_lon = (min_lon + max_lon) / 2.0

        # Determine appropriate UTM Zone (Zone 12 vs Zone 13)
        if center_lon < -108.0:
            target_epsg = 26912  # NAD83 / UTM Zone 12N
            zone_label = "UTM Zone 12N"
        else:
            target_epsg = 26913  # NAD83 / UTM Zone 13N
            zone_label = "UTM Zone 13N"

        # Define Coordinate Reference Systems
        src_srs = osr.SpatialReference()
        src_srs.ImportFromEPSG(4326) # Input: WGS84 Lat/Lon
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        tgt_srs = osr.SpatialReference()
        tgt_srs.ImportFromEPSG(target_epsg) # Output: Target UTM

        # Create Coordinate Transformer
        transform = osr.CoordinateTransformation(src_srs, tgt_srs)

        # Reproject bounding box corners from WGS84 Lat/Lon to UTM Easting/Northing meters
        utm_min_x, utm_min_y, _ = transform.TransformPoint(min_lon, min_lat)
        utm_max_x, utm_max_y, _ = transform.TransformPoint(max_lon, max_lat)

        # Flatten vector text boxes cleanly to prevent line-glitches on margins
        gdal.SetConfigOption("GDAL_PDF_RENDER_OPTIONS", "vector,text,raster")

        # Open source PDF dataset
        dataset = gdal.Open(pdf_path)
        pixel_w = dataset.RasterXSize
        pixel_h = dataset.RasterYSize

        # Define Ground Control Points (GCPs)
        gcps = [
            gdal.GCP(utm_min_x, utm_max_y, 0, 0, 0),               # Top-Left
            gdal.GCP(utm_max_x, utm_max_y, 0, pixel_w, 0),          # Top-Right
            gdal.GCP(utm_max_x, utm_min_y, 0, pixel_w, pixel_h),    # Bottom-Right
            gdal.GCP(utm_min_x, utm_min_y, 0, 0, pixel_h)           # Bottom-Left
        ]

        # Use virtual memory paths to fully isolate file processes from OS sync locks
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        vsimem_temp = f"/vsimem/{base_name}_temp.tif"
        vsimem_warp = f"/vsimem/{base_name}_warp.tif"

        # Step 1: Write raw layout pixels + GCPs out to a temporary virtual memory canvas
        translate_options_1 = gdal.TranslateOptions(format="GTiff", outputSRS=tgt_srs.ExportToWkt(), GCPs=gcps)
        gdal.Translate(vsimem_temp, dataset, options=translate_options_1)

        # Step 2: Warp the virtual canvas in RAM to enforce the aspect-ratio similarity transform
        warp_options = gdal.WarpOptions(format="GTiff", dstSRS=tgt_srs.ExportToWkt(), polynomialOrder=1)
        gdal.Warp(vsimem_warp, vsimem_temp, options=warp_options)

        # Step 3: Compile and compress the final virtual layout back into a native Geospatial PDF file
        warp_dataset = gdal.Open(vsimem_warp)
        translate_options_2 = gdal.TranslateOptions(
            format="PDF",
            creationOptions=["GEO_ENCODING=ISO32000", "COMPRESS=DEFLATE"]
        )
        gdal.Translate(output_path, warp_dataset, options=translate_options_2)
        print(f" -> Generated Warp-Corrected PDF in [{zone_label}]")

        # Close datasets and purge virtual memory instantly
        dataset = None
        warp_dataset = None
        gdal.Unlink(vsimem_temp)
        gdal.Unlink(vsimem_warp)
        return True

    except Exception as e:
        print(f"GDAL Projection Error processing {os.path.basename(pdf_path)}: {e}")
        # Safeguard: Purge memory slots if an unexpected exception strikes mid-loop
        if vsimem_temp: gdal.Unlink(vsimem_temp)
        if vsimem_warp: gdal.Unlink(vsimem_warp)
        return False

def load_coordinates_from_csv(csv_path):
    """
    Reads the CSV spreadsheet index and maps filenames to coordinate bounds.
    """
    lookup = {}
    if not os.path.exists(csv_path):
        print(f"❌ ERROR: CSV index file not found at: {csv_path}")
        return lookup

    with open(csv_path, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        row_count = 0
        for row in reader:
            row_count += 1
            try:
                bbox = (
                    float(row['min_lon'].strip()),
                    float(row['min_lat'].strip()),
                    float(row['max_lon'].strip()),
                    float(row['max_lat'].strip())
                )
                filename_key = row['filename'].strip()
                lookup[filename_key] = bbox
            except Exception as e:
                print(f"❌ Spreadsheet read error on row {row_count}: {e}")
    return lookup

def run_pipeline(target_directory, coordinate_lookup=None):
    """
    Main loop targeting maps and managing overwrite pipelines cleanly.
    """
    unref_dir = os.path.join(target_directory, "UNREFERENCED_PDFS")
    fixed_dir = os.path.join(target_directory, "FIXED_PDFS")

    for d in [unref_dir, fixed_dir]:
        if not os.path.exists(d):
            os.makedirs(d)

    print(f"Starting Batch GeoPDF Georeferencer for folder: {target_directory}\n" + "-"*60)

    files_to_process = [f for f in os.listdir(target_directory) if f.lower().endswith('.pdf')]

    for filename in files_to_process:
        full_path = os.path.join(target_directory, filename)

        if not FORCE_OVERWRITE and check_pdf_spatial(full_path):
            print(f"[SAFE] Georeferenced file confirmed and skipped: {filename}")
            continue

        print(f"[PROCESSING] Generating warp-corrected layout variables for: {filename}")

        if coordinate_lookup and filename in coordinate_lookup:
            target_bbox = coordinate_lookup[filename]
            out_name = filename.replace('.pdf', '_georef.pdf')
            target_output = os.path.join(fixed_dir, out_name)

            georeference_file_to_utm(full_path, target_output, target_bbox)
        else:
            shutil.move(full_path, os.path.join(unref_dir, filename))
            print(f" -> Isolated to UNREFERENCED_PDFS folder (No CSV record found)")

# --- MAIN ---
if __name__ == "__main__":
    maps_folder = MAPS_FOLDER
    csv_file_path = os.path.join(maps_folder, CSV_INDEX_NAME)

    coordinate_index = load_coordinates_from_csv(csv_file_path)
    run_pipeline(maps_folder, coordinate_lookup=coordinate_index)
