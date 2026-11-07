text = ("Items can be returned within 30 days for a full refund. "
        "Shipping takes 3 to 5 working days. "
        "Our stores open at 9 am and close at 6 pm.")

sentences = [s.strip() + "." for s in text.split(".") if s.strip()]

def chunk(sentences, size):
    return [" ".join(sentences[i:i + size])
            for i in range(0, len(sentences), size)]

chunks = chunk(sentences, 1)
for i, c in enumerate(chunks):
    print(i, c)
