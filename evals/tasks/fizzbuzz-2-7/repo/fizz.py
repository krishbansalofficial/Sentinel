def label(n):
    if n % 2 == 0:
        return "Fizz"
    if n % 7 == 0:
        return "Buzz"
    if n % 2 == 0 and n % 7 == 0:
        return "FizzBuzz"
    return str(n)
