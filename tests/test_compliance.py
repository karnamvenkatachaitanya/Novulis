import unittest
import sys
import os

# Add root folder and src/ folder to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root_dir)
sys.path.append(os.path.join(root_dir, "src"))

from compliance_agent.compliance_agent import filter_dom_for_target_page, validate_findings
from compliance_agent.ingest_guidelines import determine_url_path

class TestWaiverProCompliance(unittest.TestCase):
    def test_determine_url_path(self):
        # Test explicit URL routing
        self.assertEqual(determine_url_path("Settings page details", "Here is /dashboard/settings"), "/dashboard/settings")
        # Test section name keywords
        self.assertEqual(determine_url_path("Facilities listing", "Some content"), "/dashboard/facilities")
        # Test fallback content keyword matching
        self.assertEqual(determine_url_path("General Info", "Contains support tickets description"), "/dashboard/tickets")
        # Test global shared mapping
        self.assertEqual(determine_url_path("Sidebar Navigation", "Waiver logo"), "/shared")

    def test_filter_dom_for_target_page(self):
        # A mocked DOM layout matrix containing elements from other pages (bleed)
        live_dom = {
            "buttons": [
                {"selector": "button.page-my-applications-btn", "text": "App Link", "visible": True},
                {"selector": "button.page-contact-submit", "text": "Submit Contact", "visible": True}
            ]
        }
        
        # When auditing /dashboard/my-applications, the contact button should be filtered out
        filtered = filter_dom_for_target_page(live_dom, "/dashboard/my-applications")
        buttons = filtered.get("buttons", [])
        
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["selector"], "button.page-my-applications-btn")

    def test_validate_findings(self):
        # Expected compliance agent findings structure
        findings = [
            {
                "element_selector": "div.email-addr",
                "expected_behavior": "support@waiver-pro.org",
                "observed_behavior": "support@waiverpro.com",
                "severity": "critical",
                "guideline_reference": "Section 11: Support"
            },
            # This is a compliant item (false positive) and should be dropped
            {
                "element_selector": "span.brand",
                "expected_behavior": "WaiverPro",
                "observed_behavior": "WaiverPro",
                "severity": "low",
                "guideline_reference": "Section 4"
            }
        ]
        
        validated = validate_findings(findings, "/dashboard/contact")
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["element_selector"], "div.email-addr")
        self.assertEqual(validated[0]["severity"], "critical")
        self.assertEqual(validated[0]["guideline_reference"], "Section 11: Support")

if __name__ == "__main__":
    unittest.main()
