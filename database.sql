-- Table: public.users

-- DROP TABLE IF EXISTS public.users;

CREATE TABLE IF NOT EXISTS public.users
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    name character varying(150) COLLATE pg_catalog."default" NOT NULL,
    employee_code character varying(30) COLLATE pg_catalog."default" NOT NULL,
    password_hash text COLLATE pg_catalog."default" NOT NULL,
    role character varying(30) COLLATE pg_catalog."default" NOT NULL,
    department_id bigint,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT users_pkey PRIMARY KEY (id),
    CONSTRAINT users_employee_code_key UNIQUE (employee_code),
    CONSTRAINT users_department_id_fkey FOREIGN KEY (department_id)
        REFERENCES public.departments (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE RESTRICT,
    CONSTRAINT users_role_check CHECK (role::text = ANY (ARRAY['super_admin'::character varying, 'department_head'::character varying]::text[])),
    CONSTRAINT chk_user_department CHECK (role::text = 'super_admin'::text AND department_id IS NULL OR role::text = 'department_head'::text AND department_id IS NOT NULL)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.users
    OWNER to postgres;

-- Table: public.departments

-- DROP TABLE IF EXISTS public.departments;

CREATE TABLE IF NOT EXISTS public.departments
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    name character varying(100) COLLATE pg_catalog."default" NOT NULL,
    short_name character varying(20) COLLATE pg_catalog."default" NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT departments_pkey PRIMARY KEY (id),
    CONSTRAINT departments_name_key UNIQUE (name),
    CONSTRAINT departments_short_name_key UNIQUE (short_name)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.departments
    OWNER to postgres;

-- Table: public.candidates

-- DROP TABLE IF EXISTS public.candidates;

CREATE TABLE IF NOT EXISTS public.candidates
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    name character varying(150) COLLATE pg_catalog."default" NOT NULL,
    phone character varying(30) COLLATE pg_catalog."default" NOT NULL,
    email character varying(255) COLLATE pg_catalog."default" NOT NULL,
    address text COLLATE pg_catalog."default",
    nrc character varying(50) COLLATE pg_catalog."default",
    department_id bigint NOT NULL,
    cv_file_path text COLLATE pg_catalog."default" NOT NULL,
    other_attachment_path text COLLATE pg_catalog."default",
    status character varying(30) COLLATE pg_catalog."default" NOT NULL DEFAULT 'applied'::character varying,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "position" character varying(150) COLLATE pg_catalog."default" NOT NULL,
    channel character varying(100) COLLATE pg_catalog."default" NOT NULL,
    branch_id bigint,
    CONSTRAINT candidates_pkey PRIMARY KEY (id),
    CONSTRAINT candidates_branch_id_fkey FOREIGN KEY (branch_id)
        REFERENCES public.branches (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE RESTRICT,
    CONSTRAINT candidates_department_id_fkey FOREIGN KEY (department_id)
        REFERENCES public.departments (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE RESTRICT,
    CONSTRAINT candidates_status_check CHECK (status::text = ANY (ARRAY['applied'::character varying, 'interview_requested'::character varying, 'candidate_reject'::character varying, 'first_interview'::character varying, 'second_interview'::character varying, 'interview_request_reject'::character varying, 'interview_request_cancel'::character varying, 'first_interview_reject'::character varying, 'first_interview_cancel'::character varying, 'second_interview_reject'::character varying, 'second_interview_cancel'::character varying, 'offer'::character varying, 'offer_reject'::character varying, 'offer_cancel'::character varying, 'joined'::character varying, 'resigned'::character varying]::text[])),
    CONSTRAINT chk_candidate_attachment CHECK (other_attachment_path IS NOT NULL OR other_attachment_path IS NULL)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.candidates
    OWNER to postgres;
-- Index: idx_candidates_branch_id

-- DROP INDEX IF EXISTS public.idx_candidates_branch_id;

CREATE INDEX IF NOT EXISTS idx_candidates_branch_id
    ON public.candidates USING btree
    (branch_id ASC NULLS LAST)
;
-- Index: idx_candidates_channel

-- DROP INDEX IF EXISTS public.idx_candidates_channel;

CREATE INDEX IF NOT EXISTS idx_candidates_channel
    ON public.candidates USING btree
    (channel COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: idx_candidates_created_at

-- DROP INDEX IF EXISTS public.idx_candidates_created_at;

CREATE INDEX IF NOT EXISTS idx_candidates_created_at
    ON public.candidates USING btree
    (created_at ASC NULLS LAST)
;
-- Index: idx_candidates_department_id

-- DROP INDEX IF EXISTS public.idx_candidates_department_id;

CREATE INDEX IF NOT EXISTS idx_candidates_department_id
    ON public.candidates USING btree
    (department_id ASC NULLS LAST)
;
-- Index: idx_candidates_name

-- DROP INDEX IF EXISTS public.idx_candidates_name;

CREATE INDEX IF NOT EXISTS idx_candidates_name
    ON public.candidates USING btree
    (name COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: idx_candidates_position

-- DROP INDEX IF EXISTS public.idx_candidates_position;

CREATE INDEX IF NOT EXISTS idx_candidates_position
    ON public.candidates USING btree
    ("position" COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: idx_candidates_status

-- DROP INDEX IF EXISTS public.idx_candidates_status;

CREATE INDEX IF NOT EXISTS idx_candidates_status
    ON public.candidates USING btree
    (status COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: uq_candidates_email_lower

-- DROP INDEX IF EXISTS public.uq_candidates_email_lower;

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_email_lower
    ON public.candidates USING btree
    (lower(TRIM(BOTH FROM email)) COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: uq_candidates_email_normalized

-- DROP INDEX IF EXISTS public.uq_candidates_email_normalized;

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_email_normalized
    ON public.candidates USING btree
    (lower(TRIM(BOTH FROM email)) COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: uq_candidates_phone

-- DROP INDEX IF EXISTS public.uq_candidates_phone;

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_phone
    ON public.candidates USING btree
    (phone COLLATE pg_catalog."default" ASC NULLS LAST)
;
-- Index: uq_candidates_phone_normalized

-- DROP INDEX IF EXISTS public.uq_candidates_phone_normalized;

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_phone_normalized
    ON public.candidates USING btree
    (regexp_replace(phone::text, '\D'::text, ''::text, 'g'::text) COLLATE pg_catalog."default" ASC NULLS LAST)
;

-- Table: public.candidate_status_history

-- DROP TABLE IF EXISTS public.candidate_status_history;

CREATE TABLE IF NOT EXISTS public.candidate_status_history
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    candidate_id bigint NOT NULL,
    from_status character varying(30) COLLATE pg_catalog."default",
    to_status character varying(30) COLLATE pg_catalog."default" NOT NULL,
    action character varying(20) COLLATE pg_catalog."default" NOT NULL,
    performed_by_user_id bigint,
    performed_by_name character varying(150) COLLATE pg_catalog."default" NOT NULL,
    performed_role character varying(50) COLLATE pg_catalog."default" NOT NULL,
    remarks text COLLATE pg_catalog."default",
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT candidate_status_history_pkey PRIMARY KEY (id),
    CONSTRAINT candidate_status_history_candidate_id_fkey FOREIGN KEY (candidate_id)
        REFERENCES public.candidates (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE RESTRICT,
    CONSTRAINT candidate_status_history_performed_by_user_id_fkey FOREIGN KEY (performed_by_user_id)
        REFERENCES public.users (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE SET NULL,
    CONSTRAINT candidate_status_history_action_check CHECK (action::text = ANY (ARRAY['accept'::character varying, 'reject'::character varying, 'cancel'::character varying, 'skip'::character varying, 'status_change'::character varying, 'resign'::character varying]::text[])),
    CONSTRAINT candidate_status_history_to_status_check CHECK (to_status::text = ANY (ARRAY['applied'::character varying, 'interview_requested'::character varying, 'candidate_reject'::character varying, 'first_interview'::character varying, 'second_interview'::character varying, 'interview_request_reject'::character varying, 'interview_request_cancel'::character varying, 'first_interview_reject'::character varying, 'first_interview_cancel'::character varying, 'second_interview_reject'::character varying, 'second_interview_cancel'::character varying, 'offer'::character varying, 'offer_reject'::character varying, 'offer_cancel'::character varying, 'joined'::character varying, 'resigned'::character varying]::text[]))
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.candidate_status_history
    OWNER to postgres;
-- Index: idx_candidate_history_candidate_created

-- DROP INDEX IF EXISTS public.idx_candidate_history_candidate_created;

CREATE INDEX IF NOT EXISTS idx_candidate_history_candidate_created
    ON public.candidate_status_history USING btree
    (candidate_id ASC NULLS LAST, created_at DESC NULLS FIRST, id DESC NULLS FIRST)
;

-- Table: public.candidate_edit_history

-- DROP TABLE IF EXISTS public.candidate_edit_history;

CREATE TABLE IF NOT EXISTS public.candidate_edit_history
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    candidate_id bigint NOT NULL,
    changed_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    performed_by_user_id bigint,
    performed_by_name character varying(150) COLLATE pg_catalog."default" NOT NULL,
    performed_role character varying(50) COLLATE pg_catalog."default" NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT candidate_edit_history_pkey PRIMARY KEY (id),
    CONSTRAINT candidate_edit_history_candidate_id_fkey FOREIGN KEY (candidate_id)
        REFERENCES public.candidates (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE CASCADE,
    CONSTRAINT candidate_edit_history_performed_by_user_id_fkey FOREIGN KEY (performed_by_user_id)
        REFERENCES public.users (id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE SET NULL
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.candidate_edit_history
    OWNER to postgres;
-- Index: idx_candidate_edit_history_candidate_created

-- DROP INDEX IF EXISTS public.idx_candidate_edit_history_candidate_created;

CREATE INDEX IF NOT EXISTS idx_candidate_edit_history_candidate_created
    ON public.candidate_edit_history USING btree
    (candidate_id ASC NULLS LAST, created_at DESC NULLS FIRST, id DESC NULLS FIRST)
;
-- Index: idx_candidate_edit_history_changed_fields

-- DROP INDEX IF EXISTS public.idx_candidate_edit_history_changed_fields;

CREATE INDEX IF NOT EXISTS idx_candidate_edit_history_changed_fields
    ON public.candidate_edit_history USING gin
    (changed_fields)
;

-- Table: public.branches

-- DROP TABLE IF EXISTS public.branches;

CREATE TABLE IF NOT EXISTS public.branches
(
    id bigint NOT NULL GENERATED ALWAYS AS IDENTITY ( INCREMENT 1 START 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 ),
    name character varying(100) COLLATE pg_catalog."default" NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT branches_pkey PRIMARY KEY (id),
    CONSTRAINT branches_name_key UNIQUE (name)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS public.branches
    OWNER to postgres;