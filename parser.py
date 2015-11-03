import unicodecsv

headers = ['county', 'precinct', 'office', 'district', 'party', 'candidate', 'votes']

party_abbrevs = ['REP', 'DEM', 'CST', 'LIB', 'WI', 'WI2', 'WI3', 'WI4', 'WI5', 'WI6']

counties = ['ADAIR','ANDREW','ATCHISON','AUDRAIN','BARRY','BARTON','BATES','BENTON','BOLLINGER','BOONE',
    'BUCHANAN','BUTLER','CALDWELL','CALLAWAY','CAMDEN','CAPE GIRARDEAU','CARROLL','CARTER','CASS','CEDAR',
    'CHARITON','CHRISTIAN','CLARK','CLAY','CLINTON','COLE','COOPER','CRAWFORD','DADE','DALLAS','DAVIESS',
    'DEKALB','DENT','DOUGLAS','DUNKLIN','FRANKLIN','GASCONADE','GENTRY','GREENE','GRUNDY','HARRISON','HENRY',
    'HICKORY','HOLT','HOWARD','HOWELL','IRON','JACKSON','JASPER','JEFFERSON','JOHNSON','KNOX','LACLEDE',
    'LAFAYETTE','LAWRENCE','LEWIS','LINCOLN','LINN','LIVINGSTON','MCDONALD','MACON','MADISON','MARIES','MARION',
    'MERCER','MILLER','MISSISSIPPI','MONITEAU','MONROE','MONTGOMERY','MORGAN','NEW MADRID','NEWTON','NODAWAY',
    'OREGON','OSAGE','OZARK','PEMISCOT','PERRY','PETTIS','PHELPS','PIKE','PLATTE','POLK','PULASKI','PUTNAM',
    'RALLS','RANDOLPH','RAY','REYNOLDS','RIPLEY','ST CHARLES','ST CLAIR','STE GENEVIEVE','ST FRANCOIS','ST LOUIS',
    'ST LOUIS CITY','SALINE','SCHUYLER','SCOTLAND','SCOTT','SHANNON','SHELBY','STODDARD','STONE','SULLIVAN',
    'TANEY','TEXAS','VERNON','WARREN','WASHINGTON','WAYNE','WEBSTER','WORTH','WRIGHT']

def skip_check(line):
    p = False
    if line.strip() == 'General Election':
        p = True
    elif line.strip() == 'Tuesday, November 02, 2010':
        p = True
    elif line.strip() == '\n':
        p = True
    elif "County Reporting" in line:
        p = True
    elif "Official Election Returns" in line:
        p = True
    elif "State of Missouri" in line:
        p = True
    elif line.strip() == '':
        p = True
    elif 'Board on State Canvassers' in line:
        p = True
    elif 'on Tuesday, November 30, 2010' in line:
        p = True
    elif 'Office   Candidate' in line:
        p = True
    elif 'Total Votes' in line:
        p = True
    elif line[0:4] == 'Dist':
        p = True
#    elif 'Judge' in line:
#        p = True
    elif 'REGISTERED VOTERS' in line:
        p = True
    elif 'BALLOTS CAST' in line:
        p = True
    elif 'of the' in line:
        p = True
    elif 'Vote For' in line.strip():
        p = True
    elif line.strip().split("    ")[0:3] == [u'01', u'02', u'03']:
        p = True
    return p

with open('20101102__mo__general.csv', 'wb') as csvfile:
    w = unicodecsv.writer(csvfile, encoding='utf-8')
    w.writerow(headers)

    lines = open("/Users/derekwillis/Downloads/CountyGeneral2010.txt").readlines()
    keys = []
    for line in lines:
        if skip_check(line):
            continue
        if any(county in line for county in counties):
            c = [county in line for county in counties].index(True)
            county = counties[c]
        elif 'Precincts Reporting' in line:
            office = line.strip().split('Precincts Reporting')[0].strip()
            if 'District' in office:
                try:
                    office, district = office.split(' District ')
                    office = office.strip()
                except:
                    continue
            else:
                district = None
            keys = []
        else:
            candidate_bits = [x.strip() for x in line.split('   ') if x.strip() != '']
            if len(candidate_bits) == 4:
                if candidate_bits[1] == 'Yes' or candidate_bits == 'No':
                    continue
                if office != '' and candidate_bits[1] in party_abbrevs:
                    candidate, party, votes, pct = [x for x in candidate_bits if x.strip() != '']
                    votes = votes.replace(',','')
                    w.writerow([county, office, district, party, candidate, votes])
