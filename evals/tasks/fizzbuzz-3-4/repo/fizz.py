def label(n):
    if n % 3 == 0:
        return "Fizz"
    if n % 4 == 0:
        return "Buzz"
    if n % 3 == 0 and n % 4 == 0:
        return "FizzBuzz"
    return str(n)
