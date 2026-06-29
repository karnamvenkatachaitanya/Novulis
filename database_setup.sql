-- Part 1: Database Schema Setup
-- Run this in your Supabase SQL Editor to initialize the pgvector table and search functions.

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create table for storing guideline text chunks and their embeddings
CREATE TABLE IF NOT EXISTS guideline_embeddings (
    id bigserial PRIMARY KEY,
    section_name text NOT NULL,
    url_path text NOT NULL,
    content text NOT NULL,
    embedding vector(384) NOT NULL
);

-- Create an HNSW index to accelerate cosine similarity search queries
CREATE INDEX IF NOT EXISTS guideline_embeddings_embedding_idx
ON guideline_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Create Hybrid Cosine Similarity Search function (RPC)
CREATE OR REPLACE FUNCTION match_guidelines(
    query_embedding vector(384),
    filter_url_path text,
    similarity_threshold double precision,
    limit_count integer
)
RETURNS TABLE (
    id bigint,
    section_name text,
    url_path text,
    content text,
    similarity double precision
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ge.id,
        ge.section_name,
        ge.url_path,
        ge.content,
        (1 - (ge.embedding <=> query_embedding))::double precision AS similarity
    FROM
        guideline_embeddings ge
    WHERE
        (ge.url_path = filter_url_path OR ge.url_path = '/shared')
        AND (1 - (ge.embedding <=> query_embedding)) >= similarity_threshold
    ORDER BY
        ge.embedding <=> query_embedding ASC
    LIMIT
        limit_count;
END;
$$;
