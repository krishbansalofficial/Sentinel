def initials(text):
    return ''.join(w[0].upper() for w in text.split())
