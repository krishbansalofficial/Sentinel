import sys
sys.path.insert(0, '.')
from words import title_words
assert title_words('hello big world') == 'Hello Big World'
print('ok')
