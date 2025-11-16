from app import app as application

if __name__ == "__main__":
    # Simple dev entry point if you run `python wsgi.py`
    debug_mode = False
    application.run(debug=debug_mode)

