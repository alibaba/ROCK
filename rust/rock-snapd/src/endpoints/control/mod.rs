pub mod http;
pub mod protocol;

#[cfg(test)]
mod tests {
    use std::path::Path;

    #[test]
    fn domain_sources_do_not_depend_on_http_control_models() {
        fn visit(path: &Path, violations: &mut Vec<String>) {
            for entry in std::fs::read_dir(path).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    visit(&path, violations);
                } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
                    let source = std::fs::read_to_string(&path).unwrap();
                    for (line, text) in source.lines().enumerate() {
                        for dependency in
                            ["crate::vm::protocol::control", "crate::endpoints::control"]
                        {
                            if text.contains(dependency) {
                                violations.push(format!(
                                    "{}:{}: {dependency}",
                                    path.display(),
                                    line + 1
                                ));
                            }
                        }
                    }
                }
            }
        }
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut violations = Vec::new();
        visit(&source.join("vm"), &mut violations);
        visit(&source.join("snapshot"), &mut violations);
        assert!(violations.is_empty(), "{}", violations.join("\n"));
    }
}
