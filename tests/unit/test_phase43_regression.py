"""Phase 43: Regression tests for all 7 original failures."""
import pytest
from services.strict_verifier import StrictCandidateVerifier
from services.paddleocr_simple_extractor import extract_title_author
from services.paddleocr_vl_service import LayoutBlock


class TestStrictVerifierRejectsWrongCandidates:
    """Test that StrictCandidateVerifier rejects candidates with insufficient evidence."""
    
    def setup_method(self):
        self.verifier = StrictCandidateVerifier()
    
    def test_reject_author_only_match_same_author_different_title(self):
        """The Entrez dans la danse failure: same author, different title."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Entrez dans la danse",
            ocr_author="Martine Carignan",
            ocr_isbn="",
            candidate_title="Je le ferai demain",
            candidate_authors=["Martine Carignan"],
            candidate_isbn="",
        )
        assert not is_valid, "Author-only match should be rejected"
    
    def test_reject_different_author_same_title_fragment(self):
        """The Vampir failure: title is substring but author is completely different."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Vampir",
            ocr_author="Hanns Heinz Ewers",
            ocr_isbn="",
            candidate_title="Vampir der Nacht",
            candidate_authors=["Stefan Heym", "Jan Dusay"],
            candidate_isbn="",
        )
        assert not is_valid, "Different author with similar title should be rejected"
    
    def test_accept_correct_book_with_isbn(self):
        """Correct book with matching ISBN should be accepted."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Le Cœur Vert",
            ocr_author="Jean-Louis Kuffer",
            ocr_isbn="9782825104699",
            candidate_title="Le coeur vert",
            candidate_authors=["Jean-Louis Kuffer"],
            candidate_isbn="9782825104699",
        )
        assert is_valid, "Correct book with ISBN should be accepted"
        assert conf >= 0.8
    
    def test_accept_correct_book_with_title_author(self):
        """Correct book with matching title and author should be accepted."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Le Livre de Blam",
            ocr_author="Alexandre Tišma",
            ocr_isbn="",
            candidate_title="LE LIVRE DE BLAM",
            candidate_authors=["Alexandre Tišma"],
            candidate_isbn="",
        )
        assert is_valid, "Correct book with title+author should be accepted"
    
    def test_reject_surname_only_match(self):
        """Surname-only match where surname is NOT a complete word in candidate."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Random Title",
            ocr_author="Jean-Louis Kuffer",
            ocr_isbn="",
            candidate_title="Another Title",
            candidate_authors=["Kufferman"],  # "Kuffer" is substring but not complete word
            candidate_isbn="",
        )
        assert not is_valid, "Surname-only match should be rejected when not a complete word"
    
    def test_accept_surname_only_match_when_complete_word(self):
        """Surname-only match where surname IS a complete word in candidate."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Le Cœur Vert",
            ocr_author="Jean-Louis Kuffer",
            ocr_isbn="",
            candidate_title="Le Cœur Vert",
            candidate_authors=["Kuffer"],  # "Kuffer" is a complete word in candidate
            candidate_isbn="",
        )
        assert is_valid, "Surname match should be accepted when it's a complete word in candidate"
    
    def test_accept_hyphenated_author_full_match(self):
        """Jean-Marie Agier should NOT be rejected just because it contains a hyphen.
        
        A hyphen in an author's name is NOT evidence of an invalid or partial author match.
        The full extracted author agrees with the candidate.
        """
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="HISTOIRES D'AMOUR",
            ocr_author="Jean-Marie Agier",
            ocr_isbn="",
            candidate_title="HISTOIRES D'AMOUR",
            candidate_authors=["Jean-Marie Agier"],
            candidate_isbn="",
        )
        assert is_valid, "Full hyphenated author match should be accepted"
    
    def test_reject_partial_hyphenated_author(self):
        """Partial hyphenated author fragments must NOT identify a book.
        
        'Jean' or 'Marie' or 'Agier' alone should not be sufficient.
        'Jean-Marie' without 'Agier' should not be sufficient.
        """
        # "Jean" alone should not identify the book
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="HISTOIRES D'AMOUR",
            ocr_author="Jean",
            ocr_isbn="",
            candidate_title="HISTOIRES D'AMOUR",
            candidate_authors=["Jean-Marie Agier"],
            candidate_isbn="",
        )
        assert not is_valid, "Partial author fragment 'Jean' should be rejected"
        
        # "Agier" alone should not identify the book
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="HISTOIRES D'AMOUR",
            ocr_author="Agier",
            ocr_isbn="",
            candidate_title="HISTOIRES D'AMOUR",
            candidate_authors=["Jean-Marie Agier"],
            candidate_isbn="",
        )
        assert not is_valid, "Partial author fragment 'Agier' should be rejected"
        
        # "Jean-Marie" without "Agier" should not identify the book
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="HISTOIRES D'AMOUR",
            ocr_author="Jean-Marie",
            ocr_isbn="",
            candidate_title="HISTOIRES D'AMOUR",
            candidate_authors=["Jean-Marie Agier"],
            candidate_isbn="",
        )
        assert not is_valid, "Partial author fragment 'Jean-Marie' should be rejected"
    
    def test_accept_hyphenated_author_with_hyphenated_candidate(self):
        """Both OCR and candidate have the full hyphenated name."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Le Cœur Vert",
            ocr_author="Jean-Louis Kuffer",
            ocr_isbn="",
            candidate_title="Le Cœur Vert",
            candidate_authors=["Jean-Louis Kuffer"],
            candidate_isbn="",
        )
        assert is_valid, "Full hyphenated author match should be accepted"
    
    def test_reject_subject_as_author(self):
        """'de [Name]' pattern should not be treated as author."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Analyses de VIE ET MORT DE LA YOUGOSLAVIE",
            ocr_author="Pavle Ivić",
            ocr_isbn="",
            candidate_title="La Yougoslavie de Paul Garde",
            candidate_authors=["Paul Garde"],
            candidate_isbn="",
        )
        assert not is_valid, "Subject mistaken for author should be rejected"
    
    def test_reject_translator_as_author(self):
        """'traduit par [Name]' should not be treated as author."""
        is_valid, reason, conf = self.verifier.verify_candidate(
            ocr_title="Vampir",
            ocr_author="Hanns Heinz Ewers",
            ocr_isbn="",
            candidate_title="Ashaver, le juif errant",
            candidate_authors=["Jan Dusay"],
            candidate_isbn="",
        )
        assert not is_valid, "Translator mistaken for author should be rejected"


class TestPaddleOCRVlExtractor:
    """Test PaddleOCR-VL title/author extraction."""
    
    def test_extract_title_author_basic(self):
        """Basic title/author extraction from doc_title block."""
        blocks = [
            LayoutBlock(text="Jean-Louis Kuffer", bbox=[87, 440, 670, 578], label="text", score=0.95, order=0),
            LayoutBlock(text="Le Cœur Vert", bbox=[149, 589, 656, 715], label="doc_title", score=0.98, order=1),
        ]
        title, author = extract_title_author(blocks)
        assert title == "Le Cœur Vert"
        assert author == "Jean-Louis Kuffer"
    
    def test_extract_title_author_author_first(self):
        """When author comes first (HISTOIRES D'AMOUR case)."""
        blocks = [
            LayoutBlock(text="Jean-Marie Agier", bbox=[100, 300, 400, 350], label="text", score=1.0, order=0),
            LayoutBlock(text="HISTOIRES D'AMOUR", bbox=[100, 500, 600, 700], label="text", score=1.0, order=1),
        ]
        title, author = extract_title_author(blocks)
        assert "HISTOIRES" in title
        assert "Jean-Marie Agier" in author
    
    def test_extract_title_author_title_labeled(self):
        """When title has paragraph_title label."""
        blocks = [
            LayoutBlock(text="Jeanlouis Cornuz", bbox=[100, 300, 400, 350], label="text", score=1.0, order=0),
            LayoutBlock(text="Les Désastres\nde la guerre", bbox=[100, 500, 600, 700], label="paragraph_title", score=1.0, order=1),
        ]
        title, author = extract_title_author(blocks)
        assert "Les Désastres" in title
        assert "Jeanlouis Cornuz" in author
    
    def test_publisher_filtered(self):
        """Publisher should not appear in title or author."""
        blocks = [
            LayoutBlock(text="L'Age d'Homme", bbox=[100, 800, 300, 840], label="text", score=0.9, order=0),
        ]
        title, author = extract_title_author(blocks)
        assert title is None
    
    def test_empty_blocks(self):
        """Empty input should return None."""
        title, author = extract_title_author([])
        assert title is None
        assert author is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
