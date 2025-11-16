import sqlite3

# Connect to the database
conn = sqlite3.connect("investors.db")
cursor = conn.cursor()

# Fetch all investors
cursor.execute("SELECT * FROM investor;")
rows = cursor.fetchall()

# Display results
if rows:
    print("Stored investors:")
    for row in rows:
        print(row)
else:
    print("No investors found in the database.")

# Close connection
conn.close()

