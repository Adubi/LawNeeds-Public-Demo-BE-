from models import Base, KnowledgeBaseFile
from sqlalchemy import inspect

def print_model_columns():
    # Get the mapper for KnowledgeBaseFile
    mapper = inspect(KnowledgeBaseFile)
    
    print("KnowledgeBaseFile columns:")
    for column in mapper.columns:
        print(f"- {column.name}: {column.type}")

if __name__ == "__main__":
    print_model_columns()