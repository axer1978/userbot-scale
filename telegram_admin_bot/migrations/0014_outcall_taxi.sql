-- The taxi steps of an outcall (booking_flow.py).
--
-- Once the owner confirms an outcall she gets ready (booking.
-- outcall_ready_minutes) and sends READY n; the customer is then asked to
-- order a taxi to her address and send its link, which goes on to the
-- owner. She sends IN n once she is in the taxi (or the customer is told so
-- booking.outcall_in_taxi_minutes after the link came); he is then asked for
-- the exact address, floor, way in and a photo of the place. Everything he
-- sends after the confirmation is passed on to the owner.

ALTER TABLE bookings
  -- The owner sent READY n.
  ADD COLUMN ready_at          TIMESTAMPTZ,
  -- The owner was reminded to send READY n (once).
  ADD COLUMN ready_reminded_at TIMESTAMPTZ,
  -- The latest taxi link the customer sent, and when the first one came.
  ADD COLUMN taxi_link         TEXT,
  ADD COLUMN taxi_link_at      TIMESTAMPTZ,
  -- The customer was told she is in the taxi: by the owner's IN n, or by
  -- the system when no IN n came in time.
  ADD COLUMN in_taxi_at        TIMESTAMPTZ,
  ADD COLUMN in_taxi_by        TEXT CHECK (in_taxi_by IN ('owner', 'admin', 'system'));
