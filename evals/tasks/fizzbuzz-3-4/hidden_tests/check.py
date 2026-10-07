import sys
sys.path.insert(0, '.')
from fizz import label
assert label(12) == 'FizzBuzz'
assert label(3) == 'Fizz'
assert label(4) == 'Buzz'
assert label(1) == '1'
print('ok')
