package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"strings"
)

// account_contract.json is a byte-identical copy of src/accountservice/account_validation.json
// (TestContractParity enforces it). The server is authoritative; the frontend only renders its
// messages, limits and state list so forms and server errors never disagree.
//
//go:embed account_contract.json
var accountContractJSON []byte

type accountContract struct {
	Limits map[string]struct {
		Min      int `json:"min"`
		Max      int `json:"max"`
		LocalMax int `json:"local_max"`
	} `json:"limits"`
	Labels              map[string]string `json:"labels"`
	Messages            map[string]string `json:"messages"`
	USStates            []string          `json:"us_states"`
	SupportedCurrencies []string          `json:"supported_currencies"`
}

var contract = mustParseContract()

func mustParseContract() accountContract {
	var c accountContract
	if err := json.Unmarshal(accountContractJSON, &c); err != nil {
		panic(fmt.Sprintf("account_contract.json: %v", err))
	}
	return c
}

// contractMessage renders the contract message for (field, code) without echoing any input.
func contractMessage(field, code string) string {
	tmpl, ok := contract.Messages[field+"."+code]
	if !ok {
		tmpl, ok = contract.Messages[code]
	}
	if !ok {
		return "Please check this field."
	}
	label := contract.Labels[field]
	if label == "" {
		label = strings.ReplaceAll(field, "_", " ")
	}
	upper := strings.ToUpper(label[:1]) + label[1:]
	max := ""
	if l, ok := contract.Limits[field]; ok && l.Max > 0 {
		max = fmt.Sprint(l.Max)
	}
	r := strings.NewReplacer("{label}", label, "{Label}", upper, "{max}", max)
	return r.Replace(tmpl)
}
