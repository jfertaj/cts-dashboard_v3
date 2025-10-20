import psycopg2

def load_cities500(path):
    conn = psycopg2.connect(
        dbname="ctsdb", user="ctsuser", password="ctspass", host="db", port="5432"
    )
    cur = conn.cursor()

    # ✅ Check if table already has data
    cur.execute("SELECT COUNT(*) FROM geonames_cities")
    count = cur.fetchone()[0]
    if count > 0:
        print(f"⚠️ geonames_cities already has {count} rows. Skipping load.")
        cur.close()
        conn.close()
        return

    print("🧩 Loading cities500.txt into geonames_cities...")

    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.strip().split('\t')
            if len(fields) < 19:
                continue

            cur.execute("""
                INSERT INTO geonames_cities (
                    geonameid, name, asciiname, alternatenames,
                    latitude, longitude, feature_class, feature_code,
                    country_code, cc2, admin1_code, admin2_code, admin3_code, admin4_code,
                    population, elevation, dem, timezone, modification_date
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (geonameid) DO NOTHING
            """, (
                int(fields[0]),     # geonameid
                fields[1],          # name
                fields[2],          # asciiname
                fields[3],          # alternatenames
                float(fields[4]),   # latitude
                float(fields[5]),   # longitude
                fields[6],          # feature_class
                fields[7],          # feature_code
                fields[8],          # country_code
                fields[9],          # cc2
                fields[10],         # admin1_code
                fields[11],         # admin2_code
                fields[12],         # admin3_code
                fields[13],         # admin4_code
                int(fields[14]),    # population
                fields[15],         # elevation
                fields[16],         # dem
                fields[17],         # timezone
                fields[18]          # modification_date
            ))

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Finished loading cities.")

if __name__ == "__main__":
    load_cities500("/app/cities500.txt")