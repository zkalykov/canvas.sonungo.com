import os
os.environ["GRPC_DNS_RESOLVER"] = "native"

from google.cloud import firestore
from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
DATABASE_NAME = os.getenv("DATABASE_NAME")

def get_db():
    if not PROJECT_ID:
        raise ValueError("PROJECT_ID environment variable not set")
    
    if DATABASE_NAME:
        db = firestore.Client(project=PROJECT_ID, database=DATABASE_NAME)
    else:
        raise ValueError("DATABASE_NAME environment variable not set")
    
    return db

