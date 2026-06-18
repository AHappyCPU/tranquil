use std::time::Duration;

pub struct HttpResponse {
    pub status: u16,
}

pub fn post_json(
    endpoint: &str,
    headers: &[(String, String)],
    body: String,
    timeout_seconds: u64,
) -> Result<HttpResponse, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(timeout_seconds))
        .build()
        .map_err(|err| err.to_string())?;
    let mut request = client
        .post(endpoint)
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body);
    for (key, value) in headers {
        let name = reqwest::header::HeaderName::from_bytes(key.as_bytes())
            .map_err(|err| format!("invalid header name {key:?}: {err}"))?;
        let value = reqwest::header::HeaderValue::from_str(value)
            .map_err(|err| format!("invalid header value for {key:?}: {err}"))?;
        request = request.header(name, value);
    }
    let response = request.send().map_err(|err| err.to_string())?;
    let status = response.status().as_u16();
    Ok(HttpResponse { status })
}
