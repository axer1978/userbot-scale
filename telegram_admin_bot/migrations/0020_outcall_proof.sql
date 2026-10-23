-- The outcall proof (booking_flow.py, booking.outcall_proof).
--
-- Once the owner confirms an outcall, the customer is asked for a photo
-- that shows he is at the place she is going to (the hotel key card, the
-- building). His first photo after that goes on to her and is the proof:
-- he is then asked at once to order her taxi and send its link (ready_at is
-- set with it, so the taxi steps of 0014 go on from there).

ALTER TABLE bookings
  -- His proof photo reached the owner.
  ADD COLUMN proof_at TIMESTAMPTZ;
