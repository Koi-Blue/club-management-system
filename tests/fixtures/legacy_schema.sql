-- Relevant existing tables from fd26612, before comments and reimbursement files.
CREATE TABLE users (
	id INTEGER NOT NULL,
	username VARCHAR(32) NOT NULL,
	password_hash VARCHAR(200) NOT NULL,
	real_name VARCHAR(40) NOT NULL,
	phone VARCHAR(20) NOT NULL,
	college VARCHAR(40) NOT NULL,
	class_name VARCHAR(40) NOT NULL,
	department VARCHAR(40) NOT NULL,
	status VARCHAR(20) NOT NULL,
	is_admin BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE reimbursements (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	amount_cents INTEGER NOT NULL,
	reason VARCHAR(200) NOT NULL,
	status VARCHAR(20) NOT NULL,
	reviewer_id INTEGER,
	review_comment VARCHAR(200) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(reviewer_id) REFERENCES users (id)
);

CREATE TABLE journals (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	title VARCHAR(80) NOT NULL,
	body TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE forum_posts (
	id INTEGER NOT NULL,
	author_id INTEGER NOT NULL,
	title VARCHAR(80) NOT NULL,
	body TEXT NOT NULL,
	scope VARCHAR(20) NOT NULL,
	department VARCHAR(40) NOT NULL,
	direction VARCHAR(20) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(author_id) REFERENCES users (id)
);
