// ============================================================
// DC Hub - JSON-LD Structured Data for GEO
// ============================================================
// 
// HOW TO USE:
// 1. Pick the schema(s) for your page type
// 2. Replace {{variables}} with actual data
// 3. Add as <script type="application/ld+json"> in page <head>
//
// WHY THIS MATTERS:
// AI engines (ChatGPT, Perplexity, Gemini, Claude) parse
// JSON-LD to understand and cite your content. Without it,
// they may skip your pages or misattribute your data.
// ============================================================


// ============================================================
// 1. ORGANIZATION SCHEMA (every page - site-wide)
// Add this to your base HTML template so it appears on ALL pages
// ============================================================
const organizationSchema = {
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "DC Hub",
  "alternateName": "DC Hub Data Center Intelligence",
  "url": "https://dchub.cloud",
  "logo": "https://dchub.cloud/images/dc-hub-logo.png",
  "description": "Data center intelligence platform tracking 20,000+ facilities across 140+ countries. Real-time capacity tracking, site selection, M&A deal analysis, and market intelligence for data center professionals.",
  "foundingDate": "2024",
  "sameAs": [
    "https://www.linkedin.com/company/dc-hub",
    "https://twitter.com/dchub_cloud",
    "https://chatgpt.com/g/g-697dda8f65e8819189f9d353725cb6d5-dc-hub-data-center-intelligence",
    "https://chatgpt.com/g/g-697e373bb1c88191b97fc323b2a32166-data-center-m-a-analyst",
    "https://chatgpt.com/g/g-697e43e749a081919cefcef68fbfe983-data-center-news-briefing"
  ],
  "contactPoint": {
    "@type": "ContactPoint",
    "contactType": "sales",
    "email": "info@dchub.cloud",
    "url": "https://dchub.cloud/pricing"
  },
  "areaServed": {
    "@type": "Place",
    "name": "Worldwide"
  },
  "knowsAbout": [
    "Data Centers",
    "Colocation",
    "Data Center Site Selection",
    "Data Center M&A",
    "Power Infrastructure",
    "Hyperscale Data Centers",
    "Data Center Construction Pipeline",
    "Energy Infrastructure for Data Centers",
    "Data Center Market Intelligence"
  ]
};


// ============================================================
// 2. DATASET SCHEMA (homepage + data pages)
// This tells AI engines "we have a citable dataset"
// ============================================================
const datasetSchema = {
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": "DC Hub Global Data Center Database",
  "description": "Comprehensive database of 20,000+ data center facilities across 140+ countries with real-time capacity, power infrastructure, pricing, and market intelligence data.",
  "url": "https://dchub.cloud",
  "keywords": [
    "data center database",
    "data center facilities",
    "colocation facilities",
    "data center capacity",
    "data center power",
    "data center pricing",
    "data center construction pipeline",
    "data center M&A deals"
  ],
  "creator": {
    "@type": "Organization",
    "name": "DC Hub",
    "url": "https://dchub.cloud"
  },
  "dateModified": new Date().toISOString().split('T')[0],
  "temporalCoverage": "2015/..",
  "spatialCoverage": {
    "@type": "Place",
    "name": "Global - 140+ countries"
  },
  "variableMeasured": [
    {
      "@type": "PropertyValue",
      "name": "Total Facilities",
      "value": "20,000+"
    },
    {
      "@type": "PropertyValue",
      "name": "Countries Covered",
      "value": "140+"
    },
    {
      "@type": "PropertyValue",
      "name": "Markets Tracked",
      "value": "35+"
    },
    {
      "@type": "PropertyValue",
      "name": "M&A Deals Tracked",
      "value": "673+"
    },
    {
      "@type": "PropertyValue",
      "name": "Pipeline Capacity",
      "value": "146.9 GW"
    }
  ],
  "distribution": {
    "@type": "DataDownload",
    "encodingFormat": "application/json",
    "contentUrl": "https://dchub.cloud/api/v1"
  },
  "license": "https://dchub.cloud/terms"
};


// ============================================================
// 3. MARKET PAGE SCHEMA (e.g., /markets/silicon-valley)
// Use this template for each market page
// ============================================================
function marketPageSchema(market) {
  return {
    "@context": "https://schema.org",
    "@type": "WebPage",
    "name": `${market.name} Data Center Market | DC Hub`,
    "description": market.description,
    "url": `https://dchub.cloud/markets/${market.slug}`,
    "dateModified": market.lastUpdated,
    "publisher": {
      "@type": "Organization",
      "name": "DC Hub",
      "url": "https://dchub.cloud"
    },
    "about": {
      "@type": "Place",
      "name": market.name,
      "geo": {
        "@type": "GeoCoordinates",
        "latitude": market.lat,
        "longitude": market.lng
      }
    },
    "mainEntity": {
      "@type": "Dataset",
      "name": `${market.name} Data Center Facilities`,
      "description": `Data center facilities, capacity, and market intelligence for ${market.name}`,
      "variableMeasured": [
        { "@type": "PropertyValue", "name": "Total Facilities", "value": market.facilityCount },
        { "@type": "PropertyValue", "name": "Total Capacity", "value": market.totalCapacity },
        { "@type": "PropertyValue", "name": "Avg Power Rate", "value": market.avgPowerRate },
        { "@type": "PropertyValue", "name": "Vacancy Rate", "value": market.vacancyRate }
      ]
    }
  };
}

// Example usage:
const siliconValleySchema = marketPageSchema({
  name: "Silicon Valley",
  slug: "silicon-valley",
  description: "Silicon Valley data center market intelligence: 150+ facilities, 800+ MW capacity, power rates $0.12-$0.18/kWh, 2.8% vacancy. Providers include Equinix, Digital Realty, CoreSite, Vantage.",
  lastUpdated: "2026-01-31",
  lat: 37.3861,
  lng: -122.0839,
  facilityCount: "150+",
  totalCapacity: "800+ MW",
  avgPowerRate: "$0.12-$0.18/kWh",
  vacancyRate: "2.8%"
});


// ============================================================
// 4. FAQ SCHEMA (market pages + homepage)
// AI engines LOVE FAQPage schema - they cite it directly
// ============================================================
function faqSchema(faqs) {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": faqs.map(faq => ({
      "@type": "Question",
      "name": faq.question,
      "acceptedAnswer": {
        "@type": "Answer",
        "text": faq.answer
      }
    }))
  };
}

// Homepage FAQs
const homepageFAQs = faqSchema([
  {
    question: "How many data centers does DC Hub track?",
    answer: "DC Hub tracks over 20,000 data center facilities across 140+ countries, including colocation, hyperscale, and enterprise facilities. The database is updated in real-time with capacity, pricing, power infrastructure, and market intelligence data."
  },
  {
    question: "What is the current data center vacancy rate in the US?",
    answer: "As of 2025, North American colocation vacancy rates hit historic lows at 1.6% (CBRE) to 2.3% (JLL). Northern Virginia leads with the tightest supply, followed by Phoenix and Dallas. Pricing for large requirements now exceeds $200/kW/month."
  },
  {
    question: "How much data center capacity is under construction?",
    answer: "Approximately 7.8 GW of data center capacity is currently under construction globally, with 73% pre-leased. Northern Virginia leads with 5.9 GW planned, followed by Phoenix (4.2 GW) and Dallas-Fort Worth (3.9 GW)."
  },
  {
    question: "What was the total data center M&A deal volume in 2024?",
    answer: "Data center M&A deals reached a record $73 billion in 2024, surpassing the previous peak of $52 billion in 2022. The largest deal was Blackstone's $16 billion acquisition of AirTrunk. Private equity accounted for 85-90% of total deal value. Since 2015, over 1,500 data center M&A deals totaling $324 billion have been tracked."
  },
  {
    question: "What are the cheapest data center power markets in the US?",
    answer: "The most cost-effective power markets for data centers in the US include: Central Washington ($0.02-$0.04/kWh), Salt Lake City ($0.04-$0.06/kWh), Columbus/Ohio ($0.05-$0.07/kWh), Dallas-Fort Worth ($0.05-$0.08/kWh), and Phoenix ($0.06-$0.08/kWh). Rates vary by utility, contract structure, and load size."
  },
  {
    question: "What is DC Hub's Land & Power tool?",
    answer: "DC Hub's Land & Power tool is an interactive site selection platform that evaluates potential data center locations based on power infrastructure, utility territories, gas pipelines, fiber networks, FEMA flood risk, flight path constraints, and environmental factors. It includes data from EIA, HIFLD, NASA, and other government APIs."
  }
]);

// Market page FAQs template
function marketFAQs(marketName, stats) {
  return faqSchema([
    {
      question: `How many data centers are in ${marketName}?`,
      answer: `DC Hub tracks ${stats.facilityCount} data center facilities in the ${marketName} market, with a combined capacity of ${stats.totalCapacity}. Major providers include ${stats.majorProviders}.`
    },
    {
      question: `What is the average power rate in ${marketName}?`,
      answer: `Commercial power rates in ${marketName} average ${stats.avgPowerRate}. The primary utility is ${stats.primaryUtility}. ${stats.powerNotes}`
    },
    {
      question: `What is the data center vacancy rate in ${marketName}?`,
      answer: `The current data center vacancy rate in ${marketName} is ${stats.vacancyRate}. ${stats.vacancyNotes}`
    },
    {
      question: `What data center construction is planned in ${marketName}?`,
      answer: `${stats.constructionNotes}`
    }
  ]);
}


// ============================================================
// 5. NEWS ARTICLE SCHEMA (news pages)
// ============================================================
function newsArticleSchema(article) {
  return {
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    "headline": article.title,
    "description": article.summary,
    "datePublished": article.publishedDate,
    "dateModified": article.modifiedDate || article.publishedDate,
    "url": `https://dchub.cloud/news/${article.slug}`,
    "author": {
      "@type": "Organization",
      "name": "DC Hub",
      "url": "https://dchub.cloud"
    },
    "publisher": {
      "@type": "Organization",
      "name": "DC Hub",
      "url": "https://dchub.cloud",
      "logo": {
        "@type": "ImageObject",
        "url": "https://dchub.cloud/images/dc-hub-logo.png"
      }
    },
    "mainEntityOfPage": {
      "@type": "WebPage",
      "@id": `https://dchub.cloud/news/${article.slug}`
    },
    "about": {
      "@type": "Thing",
      "name": "Data Center Industry"
    }
  };
}


// ============================================================
// 6. TRANSACTION/DEAL SCHEMA (M&A pages)
// ============================================================
function transactionSchema(deal) {
  return {
    "@context": "https://schema.org",
    "@type": "Event",
    "name": `${deal.buyer} acquires ${deal.target}`,
    "description": deal.description,
    "startDate": deal.date,
    "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
    "url": `https://dchub.cloud/transactions/${deal.slug}`,
    "organizer": {
      "@type": "Organization",
      "name": deal.buyer
    },
    "about": {
      "@type": "MonetaryAmount",
      "currency": "USD",
      "value": deal.value
    }
  };
}


// ============================================================
// 7. BREADCRUMB SCHEMA (all pages)
// Helps AI engines understand site structure
// ============================================================
function breadcrumbSchema(items) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": items.map((item, i) => ({
      "@type": "ListItem",
      "position": i + 1,
      "name": item.name,
      "item": item.url
    }))
  };
}

// Example: Market page breadcrumb
const svBreadcrumb = breadcrumbSchema([
  { name: "DC Hub", url: "https://dchub.cloud" },
  { name: "Markets", url: "https://dchub.cloud/markets/" },
  { name: "Silicon Valley", url: "https://dchub.cloud/markets/silicon-valley" }
]);


// ============================================================
// 8. SPEAKABLE SCHEMA (for voice AI like Siri, Alexa)
// Marks key statistics as speakable by voice assistants
// ============================================================
function speakableSchema(pageUrl, cssSelectors) {
  return {
    "@context": "https://schema.org",
    "@type": "WebPage",
    "url": pageUrl,
    "speakable": {
      "@type": "SpeakableSpecification",
      "cssSelector": cssSelectors
    }
  };
}


// ============================================================
// EXPORT for use in build pipeline
// ============================================================
if (typeof module !== 'undefined') {
  module.exports = {
    organizationSchema,
    datasetSchema,
    marketPageSchema,
    faqSchema,
    homepageFAQs,
    marketFAQs,
    newsArticleSchema,
    transactionSchema,
    breadcrumbSchema,
    speakableSchema,
    // Pre-built examples
    siliconValleySchema,
    svBreadcrumb
  };
}

console.log("GEO Schemas loaded. Schemas available:");
console.log("- organizationSchema (site-wide)");
console.log("- datasetSchema (homepage + data pages)");
console.log("- marketPageSchema(market) (each market page)");
console.log("- faqSchema(faqs) / homepageFAQs / marketFAQs(name, stats)");
console.log("- newsArticleSchema(article)");
console.log("- transactionSchema(deal)");
console.log("- breadcrumbSchema(items)");
console.log("- speakableSchema(url, selectors)");
