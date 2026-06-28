import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Leadgen CRM Server")

# Simulated CRM database
CRM_DB = {
    "leads": [
        {
            "name": "Jane Doe",
            "email": "jane@stripe.com",
            "company": "Stripe",
            "industry": "Finance",
            "score": 85,
            "status": "Approved"
        }
    ],
    "companies": {
        "google": {"size": "100000+", "industry": "Technology", "funding": "Public"},
        "stripe": {"size": "5000+", "industry": "Finance", "funding": "Late Stage"},
        "mayo clinic": {"size": "10000+", "industry": "Healthcare", "funding": "Nonprofit"},
        "acme corp": {"size": "500+", "industry": "Manufacturing", "funding": "Series B"},
    }
}

@mcp.tool()
def search_crm(email: str) -> str:
    """Search the CRM for an existing contact/lead by their email address.
    
    Args:
        email: The email address of the lead.
    """
    for lead in CRM_DB["leads"]:
        if lead["email"].lower() == email.lower():
            return f"Found existing lead: {json.dumps(lead)}"
    return "No existing lead found with this email."

@mcp.tool()
def get_company_profile(company_name: str) -> str:
    """Fetch profile information for a company to aid in scoring (e.g., industry, size).
    
    Args:
        company_name: The name of the company to look up.
    """
    name_lower = company_name.lower()
    for key, profile in CRM_DB["companies"].items():
        if key in name_lower:
            return f"Company Profile: {json.dumps(profile)}"
    return "Company Profile: Unknown company. Defaulting to general industry."

@mcp.tool()
def create_or_update_crm_lead(
    name: str, 
    email: str, 
    company: str, 
    industry: str, 
    score: int, 
    status: str
) -> str:
    """Create a new lead or update an existing lead's score and status in the CRM.
    
    Args:
        name: Lead's name.
        email: Lead's email.
        company: Lead's company name.
        industry: Lead's industry.
        score: Score assigned to the lead (0-100).
        status: Status of the lead (e.g. 'Approved', 'Rejected', 'Needs Review').
    """
    lead_data = {
        "name": name,
        "email": email,
        "company": company,
        "industry": industry,
        "score": score,
        "status": status
    }
    
    # Check if lead already exists
    for i, lead in enumerate(CRM_DB["leads"]):
        if lead["email"].lower() == email.lower():
            CRM_DB["leads"][i] = lead_data
            return f"Successfully updated existing CRM lead for {email}."
            
    CRM_DB["leads"].append(lead_data)
    return f"Successfully created new CRM lead for {email} with status '{status}'."

if __name__ == "__main__":
    mcp.run()
