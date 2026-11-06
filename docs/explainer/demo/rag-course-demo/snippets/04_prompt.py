question = "How do I send an item back?"
best = "Items can be returned within 30 days for a full refund."

prompt = f"""Answer using only this context.

Context: {best}

Question: {question}"""

print(prompt)
