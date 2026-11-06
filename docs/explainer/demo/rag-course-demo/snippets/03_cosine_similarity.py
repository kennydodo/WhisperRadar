import numpy as np

def cosine(a, b):
    return a @ b / (np.linalg.norm(a) * np.linalg.norm(b))

# toy 2-D vectors: (about returns, about delivery)
vectors = {
    "refund policy": np.array([0.9, 0.1]),
    "shipping times": np.array([0.1, 0.9]),
    "opening hours": np.array([0.1, 0.5]),
}
query = np.array([0.8, 0.2])  # the question

for name, vec in vectors.items():
    print(f"{cosine(query, vec):.2f}  {name}")
