"""
Clean and transform 2022 Missouri election results into proper format
Converts from wide format to long format with standard columns
"""

import pdfplumber
import pandas as pd
import re
import csv

pdf_path = "2022/2022_results.pdf"
output_csv = "2022/20221108__mo__general__county_full.csv"

print(f"Opening PDF: {pdf_path}")

# Data structure to hold cleaned results
results = []

def clean_candidate_name(cell):
    """Extract party and candidate from cell text"""
    if not cell:
        return None, None
    # Remove newlines and extra spaces
    text = ' '.join(cell.split())
    # Try to extract party and candidate
    # Format is usually "Party Candidate Name" or "Party\nCandidate Name"
    parts = cell.strip().split('\n')
    if len(parts) >= 2:
        party = parts[0].strip()
        candidate = ' '.join(parts[1:]).strip()
        return party, candidate
    return None, text.strip()

def process_race_table(header_row, data_rows, office):
    """Process a single race's table and return cleaned records"""
    records = []
    
    # Parse header to get candidates
    candidates = []
    for i, cell in enumerate(header_row[1:], 1):  # Skip first column (county/label)
        if cell and cell.strip():
            party, candidate = clean_candidate_name(cell)
            candidates.append({'index': i, 'party': party, 'candidate': candidate})
    
    # Process each data row (county)
    for row in data_rows:
        if not row or not row[0]:
            continue
        
        county = row[0].strip()
        
        # Skip if it looks like a header or total row
        if county.lower() in ['county', 'total', 'totals', '']:
            continue
        
        # Extract votes for each candidate
        for cand_info in candidates:
            idx = cand_info['index']
            if idx < len(row):
                votes_str = row[idx].strip() if row[idx] else '0'
                # Remove commas and convert to int
                votes_str = votes_str.replace(',', '')
                try:
                    votes = int(votes_str)
                except:
                    votes = 0
                
                records.append({
                    'county': county,
                    'office': office,
                    'district': '',
                    'party': cand_info['party'] or '',
                    'candidate': cand_info['candidate'] or '',
                    'votes': votes
                })
    
    return records

with pdfplumber.open(pdf_path) as pdf:
    print(f"PDF has {len(pdf.pages)} pages")
    
    current_office = None
    current_table = []
    header_row = None
    
    # Extract tables from all pages
    for i, page in enumerate(pdf.pages):
        print(f"\nProcessing page {i+1}/{len(pdf.pages)}...")
        
        tables = page.extract_tables()
        
        if tables:
            for table in tables:
                if not table or len(table) < 2:
                    continue
                
                # First row might be office name or header
                first_row = table[0]
                
                # Check if this is a new race (office) header
                if first_row[0] and len(first_row[0].strip()) > 0:
                    # If it looks like an office name (long text in first cell, others are candidates)
                    if any(cell and '\n' in cell for cell in first_row[1:]):
                        # This is a header row with office and candidates
                        if current_office and current_table:
                            # Process previous table
                            records = process_race_table(header_row, current_table, current_office)
                            results.extend(records)
                            print(f"  Processed {len(records)} records for {current_office}")
                        
                        # Start new race
                        current_office = first_row[0].strip()
                        header_row = first_row
                        current_table = table[1:]  # Data rows start from second row
                    else:
                        # This is a continuation of current table
                        current_table.extend(table)
                else:
                    # Continuation of current table
                    current_table.extend(table)
    
    # Process last table
    if current_office and current_table and header_row:
        records = process_race_table(header_row, current_table, current_office)
        results.extend(records)
        print(f"  Processed {len(records)} records for {current_office}")

# Create DataFrame and save
print(f"\nCreating CSV with {len(results)} total records...")
df = pd.DataFrame(results)

# Reorder columns
df = df[['county', 'office', 'district', 'party', 'candidate', 'votes']]

# Save to CSV
df.to_csv(output_csv, index=False, encoding='utf-8')

print(f"\nExtraction complete! Saved to {output_csv}")
print(f"Total records: {len(df)}")
print(f"\nFirst few rows:")
print(df.head(10))
