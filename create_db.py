from app import app, db

# Ensure the Flask app context is set
with app.app_context():
    db.create_all()
    print("✅ Database created successfully!")
