"""
Extract county-level data from 2022 Missouri election results PDF using pdfplumber
This doesn't require Java unlike tabula-py
"""

import pdfplumber
import pandas as pd
import re
import csv

pdf_path = "Data/2022_results.pdf"
output_csv = "Data/20221108__mo__general__county_extracted.csv"

print(f"Opening PDF: {pdf_path}")

# Open the PDF and extract tables
all_data = []

with pdfplumber.open(pdf_path) as pdf:
    print(f"PDF has {len(pdf.pages)} pages")
    
    # Extract tables from each page
    for i, page in enumerate(pdf.pages):
        print(f"\nProcessing page {i+1}...")
        
        # Extract text to see what's on the page
        text = page.extract_text()
        
        # Try to extract tables
        tables = page.extract_tables()
        
        if tables:
            print(f"  Found {len(tables)} table(s) on page {i+1}")
            for j, table in enumerate(tables):
                if table:
                    print(f"    Table {j+1} has {len(table)} rows")
                    # Save each table to see the structure
                    temp_file = f"Data/2022_page_{i+1}_table_{j+1}.csv"
                    with open(temp_file, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerows(table)
                    print(f"    Saved to {temp_file}")
        else:
            print(f"  No tables found on page {i+1}")
            # Save text for manual inspection
            if text and len(text.strip()) > 0:
                text_file = f"Data/2022_page_{i+1}_text.txt"
                with open(text_file, 'w', encoding='utf-8') as f:
                    f.write(text)
                print(f"  Saved text to {text_file}")

print("\nExtraction complete! Check the Data/ folder for extracted files.")
print("You may need to inspect and manually format the data.")
