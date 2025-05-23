# config.py

# 
ADMIN_PASSWORD = 'yBSDQzeFoXEj6@3J8&g3@L'

# !!! IMPORTANT: CHANGE THIS to a long, random string for Flask sessions !!!
# You can generate a good one by running this in a Python console:
# import os
# os.urandom(24).hex()
SECRET_KEY = 'a448f4a4ea67ec5a8d975dbc5f4d6a6b2710f81721eef974'

# Define the allowed extensions for uploaded image files
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Database filename
DATABASE = 'database.db'