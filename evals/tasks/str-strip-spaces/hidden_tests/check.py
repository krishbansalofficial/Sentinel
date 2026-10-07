import sys
sys.path.insert(0, '.')
from words import strip_spaces
assert strip_spaces(' a b  c ') == 'abc'
print('ok')
