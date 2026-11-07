from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
chunks = ["Items can be returned within 30 days for a full refund.",
          "Shipping takes 3 to 5 working days.",
          "Our stores open at 9 am and close at 6 pm."]

vectorizer = TfidfVectorizer()
chunk_vectors = vectorizer.fit_transform(chunks)

query = "How do I send an item back?"
scores = cosine_similarity(vectorizer.transform([query]), chunk_vectors)[0]

for chunk, score in zip(chunks, scores):
    print(f"{score:.2f}  {chunk}")
