CREATE TABLE departments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    short_name VARCHAR(20) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public.branches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO public.branches (name) VALUES
    ('Lanthit'),
    ('Theik Pan'),
    ('Sat San'),
    ('East Dagon'),
    ('Mawlamyine'),
    ('Tampawaddy'),
    ('Hlaing Thar Yar'),
    ('Aye Thar Ya'),
    ('PRO 1 PLUS (Terminal M)'),
    ('South Dagon'),
    ('Danyin Gone'),
    ('Bago'),
    ('Mingalardon'),
    ('Head Office'),
    ('Nay Pyi Taw')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    employee_code VARCHAR(30) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role VARCHAR(30) NOT NULL
        CHECK (role IN ('super_admin', 'department_head')),
    department_id BIGINT
        REFERENCES departments(id)
        ON DELETE RESTRICT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_user_department
        CHECK (
            (role = 'super_admin' AND department_id IS NULL)
            OR
            (role = 'department_head' AND department_id IS NOT NULL)
        )
);

INSERT INTO users (
    name,
    employee_code,
    password_hash,
    role,
    department_id
)
VALUES (
    'System Administrator',
    'ADMIN001',
    'scrypt:32768:8:1$bjhaXyLj3vIfpgbK$598f94de7109b156704c63d22ad9e2fb9c20b6926b97e9ccd58a1d947bc16902e15b3e78b98a3842802105f36b84088b845c2182479eda782afd64ba3ad3974a',
    'super_admin',
    NULL
); 
-- password = "Admin@123"

CREATE TABLE IF NOT EXISTS public.candidates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    name VARCHAR(150) NOT NULL,
    phone VARCHAR(30) NOT NULL,
    email VARCHAR(255) NOT NULL,
    address TEXT,
    nrc VARCHAR(50),

    position VARCHAR(150) NOT NULL,
    channel VARCHAR(100) NOT NULL,

    department_id BIGINT NOT NULL
        REFERENCES public.departments(id)
        ON DELETE RESTRICT,

    cv_file_path TEXT NOT NULL,
    other_attachment_path TEXT,

    status VARCHAR(30) NOT NULL DEFAULT 'applied'
        CHECK (
            status IN (
                'applied',
                'interview_requested',
                'candidate_reject',
                'first_interview',
                'second_interview',
                'interview_request_reject',
                'interview_request_cancel',
                'first_interview_reject',
                'first_interview_cancel',
                'second_interview_reject',
                'second_interview_cancel',
                'offer',
                'offer_reject',
                'offer_cancel',
                'joined'
            )
        ),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_candidate_attachment CHECK (
        other_attachment_path IS NOT NULL
        OR other_attachment_path IS NULL
    )
);

-- Prevent duplicate phone numbers
-- Ignores spaces, hyphens, and other non-digit characters
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_phone_normalized
ON public.candidates (
    (regexp_replace(phone, '\D', '', 'g'))
);

-- Prevent duplicate emails
-- Ignores capitalization and surrounding spaces
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_email_normalized
ON public.candidates (
    (LOWER(TRIM(email)))
);

-- Indexes for searching and filtering
CREATE INDEX IF NOT EXISTS idx_candidates_department_id
ON public.candidates(department_id);

CREATE INDEX IF NOT EXISTS idx_candidates_status
ON public.candidates(status);

CREATE INDEX IF NOT EXISTS idx_candidates_name
ON public.candidates(name);

CREATE INDEX IF NOT EXISTS idx_candidates_created_at
ON public.candidates(created_at);

CREATE INDEX IF NOT EXISTS idx_candidates_position
ON public.candidates(position);

CREATE INDEX IF NOT EXISTS idx_candidates_channel
ON public.candidates(channel);

CREATE TABLE IF NOT EXISTS candidate_status_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    candidate_id BIGINT NOT NULL
        REFERENCES candidates(id) ON DELETE RESTRICT,

    from_status VARCHAR(30),

    to_status VARCHAR(30) NOT NULL
        CHECK (
            
            to_status IN (
                'applied',
                'interview_requested',
                'candidate_reject',
                'first_interview',
                'second_interview',
                'interview_request_reject',
                'interview_request_cancel',
                'first_interview_reject',
                'first_interview_cancel',
                'second_interview_reject',
                'second_interview_cancel',
                'offer',
                'offer_reject',
                'offer_cancel',
                'joined'
            )
        ),

    action VARCHAR(20) NOT NULL
        CHECK (
            action IN (
                'accept',
                'reject',
                'cancel',
                'skip',
                'status_change'
            )
        ),

    performed_by_user_id BIGINT
        REFERENCES users(id) ON DELETE SET NULL,

    performed_by_name VARCHAR(150) NOT NULL,
    performed_role VARCHAR(50) NOT NULL,
    remarks TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_candidate_history_candidate_created
    ON candidate_status_history(candidate_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS candidate_edit_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    candidate_id BIGINT NOT NULL
        REFERENCES candidates(id) ON DELETE CASCADE,

    changed_fields JSONB NOT NULL DEFAULT '{}'::jsonb,

    performed_by_user_id BIGINT
        REFERENCES users(id) ON DELETE SET NULL,

    performed_by_name VARCHAR(150) NOT NULL,
    performed_role VARCHAR(50) NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_candidate_edit_history_candidate_created
    ON candidate_edit_history(candidate_id, created_at DESC, id DESC);

-- Optional: for field-specific queries later
CREATE INDEX IF NOT EXISTS idx_candidate_edit_history_changed_fields
    ON candidate_edit_history USING GIN (changed_fields);

    