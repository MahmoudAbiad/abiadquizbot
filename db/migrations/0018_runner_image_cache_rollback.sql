-- TEST ONLY. Storage objects remain for explicit cleanup; never delete via SQL.
begin;
drop trigger if exists runner_queue_deleted_images on public.quizzes;
drop function if exists public.runner_queue_deleted_images();
drop table if exists public.runner_image_gc;
drop index if exists public.runner_images_expiry;
drop index if exists public.runner_rounds_retention;
drop index if exists public.runner_rounds_finished_retention;
drop index if exists public.runner_rounds_quiz;
alter table public.runner_images drop column if exists created_at;
alter table public.runner_images drop column if exists expires_at;
commit;
