/**
 * DC Hub - GEO Schema Auto-Injector
 * 
 * HOW TO USE:
 * Add this as a <script> tag in your index.html, BEFORE the closing </body> tag:
 * <script src="/js/geo-schema-injector.js"></script>
 * 
 * Or paste the contents directly in a <script> tag.
 * 
 * It auto-detects the current page and injects the right JSON-LD schemas.
 */

(function() {
  'use strict';

  const TODAY = new Date().toISOString().split('T')[0];
  const PATH = window.location.pathname;

  // ---- Helper: inject a schema into <head> ----
  function inject(schema) {
    const el = document.createElement('script');
    el.type = 'application/ld+json';
    el.textContent = JSON.stringify(schema);
    document.head.appendChild(el);
  }

  // ============================================================
  // SCHEMA 1: Organization (EVERY page)
  // ============================================================
  inject({
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "DC Hub",
    "alternateName": "DC Hub Data Center Intelligence",
    "url": "https://dchub.cloud",
    "logo": "https://dchub.cloud/images/dc-hub-logo.png",
    "description": "Data center intelligence platform tracking 20,000+ facilities across 140+ countries. Real-time capacity, site selection, M&A tracking, and market intelligence.",
    "foundingDate": "2024",
    "sameAs": [
      "https://www.linkedin.com/company/dc-hub",
      "https://chatgpt.com/g/g-697dda8f65e8819189f9d353725cb6d5-dc-hub-data-center-intelligence",
      "https://chatgpt.com/g/g-697e373bb1c88191b97fc323b2a32166-data-center-m-a-analyst",
      "https://chatgpt.com/g/g-697e43e749a081919cefcef68fbfe983-data-center-news-briefing"
    ],
    "knowsAbout": [
      "Data Centers", "Colocation", "Data Center Site Selection",
      "Data Center M&A", "Power Infrastructure", "Hyperscale Data Centers",
      "Data Center Construction Pipeline", "Data Center Market Intelligence"
    ]
  });

  // ============================================================
  // SCHEMA 2: WebSite with SearchAction (EVERY page)
  // Enables "search within DC Hub" in AI/search results
  // ============================================================
  inject({
    "@context": "https://schema.org",
    "@type": "WebSite",
    "name": "DC Hub",
    "url": "https://dchub.cloud",
    "potentialAction": {
      "@type": "SearchAction",
      "target": "https://dchub.cloud/?q={search_term_string}",
      "query-input": "required name=search_term_string"
    }
  });

  // ============================================================
  // HOMEPAGE schemas
  // ============================================================
  if (PATH === '/' || PATH === '/index.html') {

    // Dataset
    inject({
      "@context": "https://schema.org",
      "@type": "Dataset",
      "name": "DC Hub Global Data Center Database",
      "description": "Database of 20,000+ data center facilities across 140+ countries with capacity, power, pricing, pipeline, and M&A data.",
      "url": "https://dchub.cloud",
      "creator": { "@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud" },
      "dateModified": TODAY,
      "spatialCoverage": { "@type": "Place", "name": "Global - 140+ countries" },
      "variableMeasured": [
        { "@type": "PropertyValue", "name": "Total Facilities", "value": "20,000+" },
        { "@type": "PropertyValue", "name": "Countries", "value": "140+" },
        { "@type": "PropertyValue", "name": "Markets", "value": "35+" },
        { "@type": "PropertyValue", "name": "M&A Deals", "value": "673+" },
        { "@type": "PropertyValue", "name": "Pipeline Capacity", "value": "146.9 GW" },
        { "@type": "PropertyValue", "name": "NA Vacancy Rate", "value": "1.6%" },
        { "@type": "PropertyValue", "name": "Under Construction", "value": "7.8 GW" }
      ]
    });

    // FAQ (homepage)
    inject({
      "@context": "https://schema.org",
      "@type": "FAQPage",
      "mainEntity": [
        {
          "@type": "Question",
          "name": "How many data centers does DC Hub track?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "DC Hub tracks over 20,000 data center facilities across 140+ countries, including colocation, hyperscale, and enterprise facilities with real-time capacity, pricing, and power infrastructure data."
          }
        },
        {
          "@type": "Question",
          "name": "What is the current data center vacancy rate in the US?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "As of 2025, North American colocation vacancy rates are at historic lows: 1.6% (CBRE) to 2.3% (JLL). Northern Virginia has the tightest supply. Pricing for large requirements exceeds $200/kW/month."
          }
        },
        {
          "@type": "Question",
          "name": "How much data center capacity is under construction?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Approximately 7.8 GW of data center capacity is under construction globally, with 73% pre-leased. Northern Virginia leads with 5.9 GW planned, followed by Phoenix (4.2 GW) and Dallas-Fort Worth (3.9 GW)."
          }
        },
        {
          "@type": "Question",
          "name": "What was total data center M&A volume in 2024?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Data center M&A reached a record $73 billion in 2024. The largest deal was Blackstone's $16 billion acquisition of AirTrunk. Private equity comprised 85-90% of deal value. Since 2015, over 1,500 deals totaling $324 billion have been tracked by DC Hub."
          }
        },
        {
          "@type": "Question",
          "name": "What are the cheapest power markets for US data centers?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Lowest-cost power markets: Central Washington ($0.02-$0.04/kWh hydro), Salt Lake City ($0.04-$0.06/kWh), Portland/Hillsboro ($0.04-$0.07/kWh hydro), Columbus/Ohio ($0.05-$0.07/kWh), and Dallas-Fort Worth ($0.05-$0.08/kWh deregulated)."
          }
        },
        {
          "@type": "Question",
          "name": "What is DC Hub's Land and Power tool?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "DC Hub's Land and Power tool evaluates data center sites using 15+ data layers: power infrastructure (EIA), utility territories, gas pipelines, fiber networks, FEMA flood risk, flight paths, water availability, and environmental factors from government APIs."
          }
        }
      ]
    });
  }

  // ============================================================
  // MARKET PAGE schemas
  // ============================================================
  const MARKETS = {
    'northern-virginia': {
      name: 'Northern Virginia', facilities: '300+', capacity: '3,000+ MW',
      powerRate: '$0.06-$0.09/kWh', vacancy: '<1%', utility: 'Dominion Energy',
      lat: 39.0438, lng: -77.4874,
      providers: 'Equinix, Digital Realty, QTS, CyrusOne, Vantage, CloudHQ, STACK, Compass',
      description: 'Largest data center market globally. 5.9 GW planned. Known as Data Center Alley handling ~70% of global internet traffic.'
    },
    'phoenix': {
      name: 'Phoenix', facilities: '80+', capacity: '1,200+ MW',
      powerRate: '$0.06-$0.08/kWh', vacancy: '2.1%', utility: 'APS, SRP',
      lat: 33.4484, lng: -112.0740,
      providers: 'CyrusOne, QTS, Aligned, Vantage, Stream, Digital Realty, Compass',
      description: 'Fastest-growing US data center market. 4.2 GW planned. Major hyperscale expansion hub.'
    },
    'dallas-fort-worth': {
      name: 'Dallas-Fort Worth', facilities: '150+', capacity: '1,500+ MW',
      powerRate: '$0.05-$0.08/kWh', vacancy: '1.8%', utility: 'Oncor (deregulated)',
      lat: 32.7767, lng: -96.7970,
      providers: 'CyrusOne, QTS, Digital Realty, Flexential, DataBank, Compass, T5',
      description: 'Major hyperscale hub. 3.9 GW planned. Deregulated energy market enables competitive power procurement.'
    },
    'silicon-valley': {
      name: 'Silicon Valley', facilities: '150+', capacity: '800+ MW',
      powerRate: '$0.12-$0.18/kWh', vacancy: '2.8%', utility: 'PG&E',
      lat: 37.3861, lng: -122.0839,
      providers: 'Equinix, Digital Realty, CoreSite, Vantage, Hurricane Electric',
      description: 'Premium connectivity market. 150+ facilities. Highest power costs in US. Power-constrained.'
    },
    'chicago': {
      name: 'Chicago', facilities: '80+', capacity: '600+ MW',
      powerRate: '$0.07-$0.10/kWh', vacancy: '3.2%', utility: 'ComEd',
      lat: 41.8781, lng: -87.6298,
      providers: 'Digital Realty, Equinix, QTS, CyrusOne, DataBank',
      description: 'Central US hub for financial services. 350 E. Cermak is one of most connected buildings globally.'
    },
    'atlanta': {
      name: 'Atlanta', facilities: '50+', capacity: '400+ MW',
      powerRate: '$0.08-$0.11/kWh', vacancy: '4.1%', utility: 'Georgia Power',
      lat: 33.7490, lng: -84.3880,
      providers: 'QTS, Switch, Digital Realty, Flexential',
      description: 'Southeast US hub. QTS Atlanta Metro mega-campus is one of the largest in the US.'
    },
    'portland': {
      name: 'Portland/Hillsboro', facilities: '40+', capacity: '500+ MW',
      powerRate: '$0.04-$0.07/kWh', vacancy: '3.5%', utility: 'PGE/BPA',
      lat: 45.5152, lng: -122.6784,
      providers: 'Aligned, EdgeCore, Stack, Vantage',
      description: 'Low-cost hydro power from BPA. 1,200+ MW planned. Oregon tax incentives.'
    },
    'salt-lake-city': {
      name: 'Salt Lake City', facilities: '20+', capacity: '200+ MW',
      powerRate: '$0.04-$0.06/kWh', vacancy: '5.0%', utility: 'Rocky Mountain Power',
      lat: 40.7608, lng: -111.8910,
      providers: 'Aligned, Novva, C7, Cyxtera',
      description: 'Emerging market with very low power costs. Good seismic profile. Cool climate.'
    },
    'frankfurt': {
      name: 'Frankfurt', facilities: '100+', capacity: '800+ MW',
      powerRate: '€0.15-€0.20/kWh', vacancy: '5.2%', utility: 'Various',
      lat: 50.1109, lng: 8.6821,
      providers: 'Equinix, Digital Realty/Interxion, NTT, CyrusOne, AtlasEdge',
      description: 'Largest European data center market. Home to DE-CIX, the worlds largest internet exchange.'
    },
    'london': {
      name: 'London', facilities: '80+', capacity: '700+ MW',
      powerRate: '£0.14-£0.20/kWh', vacancy: '4.5%', utility: 'Various',
      lat: 51.5074, lng: -0.1278,
      providers: 'Equinix, Digital Realty, Virtus, Ark, CyrusOne',
      description: 'Major European financial services data center hub. Slough, Docklands, West London clusters.'
    },
    'singapore': {
      name: 'Singapore', facilities: '70+', capacity: '500+ MW',
      powerRate: 'S$0.15-S$0.20/kWh', vacancy: '3.0%', utility: 'Various',
      lat: 1.3521, lng: 103.8198,
      providers: 'Equinix, Digital Realty, ST Telemedia, Keppel DC, AirTrunk',
      description: 'Key APAC interconnection hub. Government moratorium partially lifted with green requirements.'
    },
    'tokyo': {
      name: 'Tokyo', facilities: '100+', capacity: '900+ MW',
      powerRate: '$0.10-$0.15/kWh', vacancy: '3.8%', utility: 'TEPCO',
      lat: 35.6762, lng: 139.6503,
      providers: 'Equinix, Digital Realty, NTT, KDDI, Colt DCS, AirTrunk',
      description: 'Largest APAC data center market. Inzai City major campus location. Seismic engineering requirements.'
    }
  };

  // Check if current page is a market page
  const marketMatch = PATH.match(/^\/markets\/([a-z-]+)\/?$/);
  if (marketMatch && MARKETS[marketMatch[1]]) {
    const m = MARKETS[marketMatch[1]];
    const slug = marketMatch[1];

    // Market WebPage + Dataset
    inject({
      "@context": "https://schema.org",
      "@type": "WebPage",
      "name": m.name + " Data Center Market | DC Hub",
      "description": m.name + " data center market: " + m.facilities + " facilities, " + m.capacity + " capacity, " + m.powerRate + " power, " + m.vacancy + " vacancy. " + m.providers + ".",
      "url": "https://dchub.cloud/markets/" + slug,
      "dateModified": TODAY,
      "publisher": { "@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud" },
      "about": {
        "@type": "Place",
        "name": m.name,
        "geo": { "@type": "GeoCoordinates", "latitude": m.lat, "longitude": m.lng }
      },
      "mainEntity": {
        "@type": "Dataset",
        "name": m.name + " Data Center Facilities",
        "variableMeasured": [
          { "@type": "PropertyValue", "name": "Total Facilities", "value": m.facilities },
          { "@type": "PropertyValue", "name": "Total Capacity", "value": m.capacity },
          { "@type": "PropertyValue", "name": "Avg Power Rate", "value": m.powerRate },
          { "@type": "PropertyValue", "name": "Vacancy Rate", "value": m.vacancy }
        ]
      }
    });

    // Market Breadcrumb
    inject({
      "@context": "https://schema.org",
      "@type": "BreadcrumbList",
      "itemListElement": [
        { "@type": "ListItem", "position": 1, "name": "DC Hub", "item": "https://dchub.cloud" },
        { "@type": "ListItem", "position": 2, "name": "Markets", "item": "https://dchub.cloud/markets/" },
        { "@type": "ListItem", "position": 3, "name": m.name, "item": "https://dchub.cloud/markets/" + slug }
      ]
    });

    // Market FAQ
    inject({
      "@context": "https://schema.org",
      "@type": "FAQPage",
      "mainEntity": [
        {
          "@type": "Question",
          "name": "How many data centers are in " + m.name + "?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "DC Hub tracks " + m.facilities + " data center facilities in " + m.name + " with " + m.capacity + " total capacity. Major providers include " + m.providers + "."
          }
        },
        {
          "@type": "Question",
          "name": "What is the average power rate for data centers in " + m.name + "?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Commercial/industrial power rates in " + m.name + " average " + m.powerRate + " from " + m.utility + "."
          }
        },
        {
          "@type": "Question",
          "name": "What is the data center vacancy rate in " + m.name + "?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "The current data center vacancy rate in " + m.name + " is " + m.vacancy + ". " + m.description
          }
        }
      ]
    });
  }

  // ============================================================
  // TRANSACTIONS page
  // ============================================================
  if (PATH.startsWith('/transactions') || PATH.startsWith('/transaction-comps') || PATH.startsWith('/ai-deals')) {
    inject({
      "@context": "https://schema.org",
      "@type": "Dataset",
      "name": "DC Hub Data Center M&A Transaction Database",
      "description": "673+ data center M&A transactions since 2015 totaling $324B+. 2024 record: $73B. Includes buyer, seller, deal value, capacity, and valuation multiples.",
      "url": "https://dchub.cloud/transactions",
      "dateModified": TODAY,
      "creator": { "@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud" },
      "variableMeasured": [
        { "@type": "PropertyValue", "name": "Total Deals", "value": "673+" },
        { "@type": "PropertyValue", "name": "Aggregate Value", "value": "$324B+" },
        { "@type": "PropertyValue", "name": "2024 Volume", "value": "$73B (record)" },
        { "@type": "PropertyValue", "name": "Largest 2024 Deal", "value": "Blackstone → AirTrunk ($16B)" }
      ]
    });
  }

  // ============================================================
  // NEWS page
  // ============================================================
  if (PATH.startsWith('/news')) {
    inject({
      "@context": "https://schema.org",
      "@type": "CollectionPage",
      "name": "Data Center News | DC Hub",
      "description": "Real-time data center industry news aggregated from 40+ sources. Hyperscale, colocation, M&A, power, AI infrastructure, and construction updates.",
      "url": "https://dchub.cloud/news",
      "publisher": { "@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud" }
    });
  }

  // ============================================================
  // LAND & POWER page
  // ============================================================
  if (PATH.startsWith('/land-power')) {
    inject({
      "@context": "https://schema.org",
      "@type": "SoftwareApplication",
      "name": "DC Hub Land & Power Tool",
      "description": "Interactive data center site selection tool with 15+ data layers: power infrastructure (EIA), utility territories, gas pipelines, fiber networks, FEMA flood risk, flight paths, and environmental data.",
      "url": "https://dchub.cloud/land-power",
      "applicationCategory": "BusinessApplication",
      "operatingSystem": "Web Browser",
      "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD",
        "description": "Free tier available with premium features"
      }
    });
  }

  // ============================================================
  // PRICING page
  // ============================================================
  if (PATH.startsWith('/pricing')) {
    inject({
      "@context": "https://schema.org",
      "@type": "WebPage",
      "name": "DC Hub Pricing | API Access & Subscriptions",
      "description": "DC Hub API and platform pricing. Explorer (Free, 100 req/month), Professional ($99/month, 10,000 req), Enterprise (custom, unlimited + raw exports).",
      "url": "https://dchub.cloud/pricing"
    });
  }

  console.log('[DC Hub GEO] Schema injection complete for: ' + PATH);

})();
