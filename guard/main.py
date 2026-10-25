from fastapi import FastAPI
from pydantic import BaseModel
from fastapi import HTTPException

app = FastAPI()

@app.get("/books/count")

def count_books():
    return {"count": 3}


class NewBook(BaseModel):
    title: str
    pages: int
    isbn: str

@app.post("/books")

def add_book(book: NewBook):
    return {"added": book.title}

BOOKS = {

    1: {"title": "The Lord of the Rings", "pages": 1178, "isbn": "978-0547928227"},
    2: {"title": "The Great Gatsby", "pages": 180, "isbn": "978-0743273565"},
    3: {"title": "The Catcher in the Rye", "pages": 224, "isbn": "978-0316769488"},

}


app.get("/books/{book_id}")
def get_book(book_id: int):
    if book_id not in BOOKS:
        raise HTTPException(status_code=404, detail="Book not found")
    return BOOKS[book_id]


