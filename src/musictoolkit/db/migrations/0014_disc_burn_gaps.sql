-- Music CDs: how the songs follow one another on the disc. 'none': they run on from one another (the disc is written in one go); '2s': two
-- seconds of silence lie between them (written song by song). NULL for what is not a music CD, and for the burns made before this was recorded
-- (those all had two seconds).
ALTER TABLE disc_burns ADD COLUMN gaps TEXT CHECK(gaps IS NULL OR gaps IN ('none', '2s'));
