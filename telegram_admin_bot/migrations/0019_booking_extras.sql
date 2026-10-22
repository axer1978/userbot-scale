-- The extras the customer asked for (booking_flow.py), read from the chat
-- with the time and shown in the request to the owner ("Extras: ...").

ALTER TABLE bookings
  ADD COLUMN extras TEXT NOT NULL DEFAULT '';
