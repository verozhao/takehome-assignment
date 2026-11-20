Revenue: the sum of `payment.amount`. A payment belongs to the month of its `payment_date`.

Late return: a rental whose `return_date` is later than its `rental_date` plus the film's
`rental_duration` in days. A rental that has not been returned yet is not late or on time.
It is still out.
